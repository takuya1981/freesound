# -*- coding: utf-8 -*-

#
# Freesound is (c) MUSIC TECHNOLOGY GROUP, UNIVERSITAT POMPEU FABRA
#
# Freesound is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# Freesound is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Authors:
#     See AUTHORS file.
#


from rest_framework.response import Response
from rest_framework.decorators import api_view, authentication_classes
from rest_framework.exceptions import ParseError
from provider.oauth2.models import AccessToken, Grant
from apiv2.serializers import *
from apiv2.authentication import OAuth2Authentication, TokenAuthentication, SessionAuthentication
from utils import GenericAPIView, ListAPIView, RetrieveAPIView, WriteRequiredGenericAPIView, DownloadAPIView, get_analysis_data_for_queryset_or_sound_ids, create_sound_object, api_search, ApiSearchPaginator, get_sounds_descriptors, prepend_base
from exceptions import *
from forms import *
from models import ApiV2Client
from api.models import ApiKey
from bookmarks.models import Bookmark, BookmarkCategory
from api.forms import ApiKeyForm
from accounts.views import handle_uploaded_file
from search.views import search_prepare_query, search_prepare_sort
from freesound.utils.filesystem import generate_tree
from freesound.utils.search.solr import Solr, SolrException, SolrResponseInterpreter, SolrResponseInterpreterPaginator
from freesound.utils.nginxsendfile import sendfile
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.http import HttpResponseRedirect, Http404, HttpResponse
from django.core.urlresolvers import reverse
from urllib import unquote
import settings
import logging
import datetime
import os


logger = logging.getLogger("api")


####################################
# SEARCH AND SIMILARITY SEARCH VIEWS
####################################

class Search(GenericAPIView):
    """
    Sound search.
    TODO: proper doccumentation.
    """

    def get(self, request,  *args, **kwargs):
        # Validate search form and check page 0
        search_form = SoundSearchFormAPI(request.QUERY_PARAMS)
        if not search_form.is_valid():
            raise ParseError
        if search_form.cleaned_data['page'] < 1:
            raise NotFoundException

        try:
            # Get search results
            solr = Solr(settings.SOLR_URL)
            query = search_prepare_query(unquote(search_form.cleaned_data['query']),
                                         unquote(search_form.cleaned_data['filter']),
                                         search_prepare_sort(search_form.cleaned_data['sort'], SEARCH_SORT_OPTIONS_API),
                                         search_form.cleaned_data['page'],
                                         search_form.cleaned_data['page_size'],
                                         grouping=search_form.cleaned_data['group_by_pack'],
                                         include_facets=False)
            results = SolrResponseInterpreter(solr.select(unicode(query)))

            # Paginate results
            paginator = SolrResponseInterpreterPaginator(results, search_form.cleaned_data['page_size'])
            if search_form.cleaned_data['page'] > paginator.num_pages:
                raise NotFoundException
            page = paginator.page(search_form.cleaned_data['page'])
            response_data = dict()
            response_data['count'] = paginator.count
            response_data['previous'] = None
            response_data['next'] = None
            if page['has_other_pages']:
                if page['has_previous']:
                    response_data['previous'] = search_form.construct_link(reverse('apiv2-sound-search'), page=page['previous_page_number'])
                if page['has_next']:
                    response_data['next'] = search_form.construct_link(reverse('apiv2-sound-search'), page=page['next_page_number'])

            # Get analysis data and serialize sound results
            get_analysis_data_for_queryset_or_sound_ids(self, sound_ids=[object['id'] for object in page['object_list']])
            sounds = []
            for object in page['object_list']:
                try:
                    sound = SoundListSerializer(Sound.objects.select_related('user').get(id=object['id']), context=self.get_serializer_context()).data
                    if 'more_from_pack' in object.keys():
                        if object['more_from_pack'] > 0:
                            sound['more_from_same_pack'] = search_form.construct_link(reverse('apiv2-sound-search'), page=1, filter='grouping_pack:"%i_%s"' % (int(object['pack_id']), object['pack_name']), group_by_pack='0')
                            sound['n_from_same_pack'] = object['more_from_pack'] + 1  # we add one as is the sound itself
                    sounds.append(sound)
                except:
                    # This will happen if there are synchronization errors between solr index and the database.
                    # In that case sounds are are set to null
                    sounds.append(None)
            response_data['results'] = sounds

        except SolrException:
                raise ServerErrorException

        return Response(response_data, status=status.HTTP_200_OK)


class CombinedSearch(GenericAPIView):
    """
    Sound advanced search.
    TODO: proper documentation.
    """

    serializer_class = SimilarityFileSerializer
    analysis_file = None

    def get(self, request,  *args, **kwargs):
        # Validate search form and check page 0
        search_form = SoundCombinedSearchFormAPI(request.QUERY_PARAMS)
        if not search_form.is_valid():
            raise ParseError
        if search_form.cleaned_data['page'] < 1:
                raise NotFoundException

        # Get search results
        analysis_file = None
        if self.analysis_file:
            analysis_file = self.analysis_file.read()
        results, count, distance_to_target_data, more_from_pack_data = api_search(search_form, target_file=analysis_file)

        # Paginate results
        paginator = ApiSearchPaginator(results, count, search_form.cleaned_data['page_size'])
        if search_form.cleaned_data['page'] > paginator.num_pages and count != 0:
            raise NotFoundException
        page = paginator.page(search_form.cleaned_data['page'])
        response_data = dict()
        if self.analysis_file:
            response_data['target_analysis_file'] = '%s (%i KB)' % (self.analysis_file._name, self.analysis_file._size/1024)
        response_data['count'] = paginator.count
        response_data['previous'] = None
        response_data['next'] = None
        if page['has_other_pages']:
                if page['has_previous']:
                    response_data['previous'] = search_form.construct_link(reverse('apiv2-sound-combined-search'), page=page['previous_page_number'])
                if page['has_next']:
                    response_data['next'] = search_form.construct_link(reverse('apiv2-sound-combined-search'), page=page['next_page_number'])

        # Get analysis data and serialize sound results
        get_analysis_data_for_queryset_or_sound_ids(self, sound_ids=page['object_list'])
        sounds = []
        for sound_id in page['object_list']:
            try:
                sound = SoundListSerializer(Sound.objects.select_related('user').get(id=sound_id), context=self.get_serializer_context()).data
                # Distance to target is present we add it to the serialized sound
                if distance_to_target_data:
                    sound['distance_to_target'] = distance_to_target_data[sound_id]
                if more_from_pack_data:
                    if more_from_pack_data[sound_id][0]:
                        sound['more_from_same_pack'] = search_form.construct_link(reverse('apiv2-sound-combined-search'), page=1, filter='grouping_pack:"%i_%s"' % (int(more_from_pack_data[sound_id][1]), more_from_pack_data[sound_id][2]), group_by_pack='0')
                        sound['n_from_same_pack'] = more_from_pack_data[sound_id][0] + 1  # we add one as is the sound itself
                sounds.append(sound)

            except:
                # This will happen if there are synchronization errors between solr, gaia and and the database.
                # In that case sounds are are set to null
                sounds.append(None)
        response_data['results'] = sounds

        return Response(response_data, status=status.HTTP_200_OK)

    def post(self, request,  *args, **kwargs):
        # This view has a post version to handle analysis file uploads
        serializer = SimilarityFileSerializer(data=request.DATA, files=request.FILES)
        if serializer.is_valid():
            analysis_file = request.FILES['analysis_file']
            self.analysis_file = analysis_file
            return self.get(request,  *args, **kwargs)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SimilaritySearchWithTargetFile(GenericAPIView):
    """
    Return similarity sounds to an uploaded analysis file.
    TODO: proper documentation.
    """

    serializer_class = SimilarityFileSerializer

    def post(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")

        serializer = SimilarityFileSerializer(data=request.DATA, files=request.FILES)
        if serializer.is_valid():
            analysis_file = request.FILES['analysis_file']

            # Validate search form and check page 0
            similarity_file_form = SimilarityFormAPI(request.QUERY_PARAMS)
            if not similarity_file_form.is_valid():
                raise ParseError
            if similarity_file_form.cleaned_data['page'] < 1:
                    raise NotFoundException

            # Get gaia results
            results, count, distance_to_target_data, more_from_pack_data = api_search(similarity_file_form, target_file=analysis_file.read())

            # Paginate results
            paginator = ApiSearchPaginator(results, count, similarity_file_form.cleaned_data['page_size'])
            if similarity_file_form.cleaned_data['page'] > paginator.num_pages and count != 0:
                raise NotFoundException
            page = paginator.page(similarity_file_form.cleaned_data['page'])
            response_data = dict()
            response_data['target_analysis_file'] = '%s (%i KB)' % (analysis_file._name, analysis_file._size/1024)
            response_data['count'] = paginator.count
            response_data['previous'] = None
            response_data['next'] = None
            if page['has_other_pages']:
                    if page['has_previous']:
                        response_data['previous'] = similarity_file_form.construct_link(reverse('apiv2-similarity-file'), page=page['previous_page_number'])
                    if page['has_next']:
                        response_data['next'] = similarity_file_form.construct_link(reverse('apiv2-similarity-file'), page=page['next_page_number'])

            # Get analysis data and serialize sound results
            get_analysis_data_for_queryset_or_sound_ids(self, sound_ids=page['object_list'])
            sounds = []
            for sound_id in page['object_list']:
                try:
                    sound = SoundListSerializer(Sound.objects.select_related('user').get(id=sound_id), context=self.get_serializer_context()).data
                    # Distance to target is present we add it to the serialized sound
                    if distance_to_target_data:
                        sound['distance_to_target'] = distance_to_target_data[sound_id]
                    sounds.append(sound)

                except:
                    # This will happen if there are synchronization errors between gaia and and the database.
                    # In that case sounds are are set to null
                    sounds.append(None)
            response_data['results'] = sounds
            return Response(response_data, status=status.HTTP_200_OK)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


#############
# SOUND VIEWS
#############

class SoundInstance(RetrieveAPIView):
    """
    Detailed sound information.
    TODO: proper doccumentation.
    """
    serializer_class = SoundSerializer
    queryset = Sound.objects.filter(moderation_state="OK", processing_state="OK")

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        return super(SoundInstance, self).get(request, *args, **kwargs)


class SoundAnalysis(GenericAPIView):
    """
    Sound analysis information.
    TODO: proper doccumentation.
    """

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")

        sound_id = kwargs['pk']
        descriptors = []
        if request.QUERY_PARAMS.get('descriptors', False):
            descriptors = request.QUERY_PARAMS['descriptors'].split(',')
        response_data = get_sounds_descriptors([sound_id],
                                                descriptors,
                                                request.QUERY_PARAMS.get('normalized', '0') == '1',
                                                only_leaf_descriptors=True)
        if response_data:
            return Response(response_data[str(sound_id)], status=status.HTTP_200_OK)
        else:
            raise InvalidUrlException


class SimilarSounds(GenericAPIView):
    """
    Return similarity sounds to a given sound id
    TODO: proper documentation.
    """

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")

        # Validate search form and check page 0
        similarity_sound_form = SimilarityFormAPI(request.QUERY_PARAMS)
        if not similarity_sound_form.is_valid():
            raise ParseError
        if similarity_sound_form.cleaned_data['page'] < 1:
                raise NotFoundException

        # Get search results
        sound_id = self.kwargs['pk']
        similarity_sound_form.cleaned_data['target'] = str(sound_id)
        results, count, distance_to_target_data, more_from_pack_data = api_search(similarity_sound_form)

        # Paginate results
        paginator = ApiSearchPaginator(results, count, similarity_sound_form.cleaned_data['page_size'])
        if similarity_sound_form.cleaned_data['page'] > paginator.num_pages and count != 0:
            raise NotFoundException
        page = paginator.page(similarity_sound_form.cleaned_data['page'])
        response_data = dict()
        response_data['count'] = paginator.count
        response_data['previous'] = None
        response_data['next'] = None
        if page['has_other_pages']:
                if page['has_previous']:
                    response_data['previous'] = similarity_sound_form.construct_link(reverse('apiv2-similarity-sound', args=[sound_id]), page=page['previous_page_number'])
                if page['has_next']:
                    response_data['next'] = similarity_sound_form.construct_link(reverse('apiv2-similarity-sound', args=[sound_id]), page=page['next_page_number'])

        # Get analysis data and serialize sound results
        get_analysis_data_for_queryset_or_sound_ids(self, sound_ids=page['object_list'])
        sounds = []
        for sound_id in page['object_list']:
            try:
                sound = SoundListSerializer(Sound.objects.select_related('user').get(id=sound_id), context=self.get_serializer_context()).data
                # Distance to target is present we add it to the serialized sound
                if distance_to_target_data:
                    sound['distance_to_target'] = distance_to_target_data[sound_id]
                sounds.append(sound)

            except:
                # This will happen if there are synchronization errors between gaia and and the database.
                # In that case sounds are are set to null
                sounds.append(None)
        response_data['results'] = sounds
        return Response(response_data, status=status.HTTP_200_OK)


class SoundComments(ListAPIView):
    """
    List of comments of a user.
    TODO: proper documentation.
    """
    serializer_class = SoundCommentsSerializer

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        return super(SoundComments, self).get(request, *args, **kwargs)

    def get_queryset(self):
        return Comment.objects.filter(object_id=self.kwargs['pk'])


class SoundRatings(ListAPIView):
    """
    List of ratings of a user.
    TODO: proper documentation.
    """
    serializer_class = SoundRatingsSerializer

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        return super(SoundRatings, self).get(request, *args, **kwargs)

    def get_queryset(self):
        return Rating.objects.filter(object_id=self.kwargs['pk'])


class SoundsFromListOfIds(ListAPIView):
    """
    Return a list of sounds for the specified IDS (so developers can get all information at once)
    Ids must be indicated with parameter ids and a list of comma-separated numbers.
    """

    serializer_class = SoundListSerializer

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        return super(SoundsFromListOfIds, self).get(request, *args, **kwargs)

    def get_queryset(self):
        try:
            ids = [int(item) for item in self.request.QUERY_PARAMS.get('ids').split(',')[:settings.REST_FRAMEWORK['MAX_PAGINATE_BY']]]
        except:
            ids = []
        return Sound.objects.filter(id__in=ids)


class DownloadSound(DownloadAPIView):
    """
    Download a sound.
    """

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")

        sound_id = kwargs['pk']
        try:
            sound = Sound.objects.get(id=sound_id, moderation_state="OK", processing_state="OK")
        except Sound.DoesNotExist:
            raise NotFoundException

        if not os.path.exists(sound.locations('path')):
            raise NotFoundException

        return sendfile(sound.locations("path"), sound.friendly_filename(), sound.locations("sendfile_url"))


############
# USER VIEWS
############

class UserInstance(RetrieveAPIView):
    """
    Detailed user information.
    TODO: proper documentation.
    """
    lookup_field = "username"
    serializer_class = UserSerializer
    queryset = User.objects.filter(is_active=True)

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        return super(UserInstance, self).get(request, *args, **kwargs)


class UserSounds(ListAPIView):
    """
    List of sounds uploaded by user.
    TODO: proper documentation.
    """
    lookup_field = "username"
    serializer_class = SoundListSerializer

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        return super(UserSounds, self).get(request, *args, **kwargs)

    def get_queryset(self):
        try:
            User.objects.get(username=self.kwargs['username'], is_active=True)
        except User.DoesNotExist:
            raise NotFoundException

        queryset = Sound.objects.select_related('user').filter(moderation_state="OK",
                                                               processing_state="OK",
                                                               user__username=self.kwargs['username'])
        get_analysis_data_for_queryset_or_sound_ids(self, queryset=queryset)
        return queryset


class UserPacks (ListAPIView):
    """
    List of packs uploaded by user.
    TODO: proper documentation.
    """
    serializer_class = PackSerializer
    queryset = Pack.objects.all()

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        return super(UserPacks, self).get(request, *args, **kwargs)

    def get_queryset(self):
        try:
            User.objects.get(username=self.kwargs['username'], is_active=True)
        except User.DoesNotExist:
            raise NotFoundException

        queryset = Pack.objects.select_related('user').filter(user__username=self.kwargs['username'])
        return queryset


class UserBookmarkCategories(ListAPIView):
    """
    List of bookmarks of a user.
    TODO: proper documentation.
    """
    serializer_class = BookmarkCategorySerializer

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        return super(UserBookmarkCategories, self).get(request, *args, **kwargs)

    def get_queryset(self):
        categories = BookmarkCategory.objects.filter(user__username=self.kwargs['username'])
        user = User.objects.get(username=self.kwargs['username'])
        uncategorized = BookmarkCategory(name='Uncategorized',user=user,id=0)
        return [uncategorized] + list(categories)


class UserSoundsForBookmarkCategory(ListAPIView):
    """
    List of sounds of a bookmark category of a user.
    TODO: proper documentation.
    """
    serializer_class = SoundListSerializer

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        return super(UserSoundsForBookmarkCategory, self).get(request, *args, **kwargs)

    def get_queryset(self):

        kwargs = dict()
        kwargs['user__username'] = self.kwargs['username']

        if 'category_id' in self.kwargs:
            #category = BookmarkCategory.objects.get(user__username=self.kwargs['username'], id=self.kwargs['category_id'])
            kwargs['category__id'] = self.kwargs['category_id']
        else:
            kwargs['category'] = None
        #else:
        #    return  [bookmark.sound for bookmark in Bookmark.objects.select_related("sound").filter(user__username=self.kwargs['username'],category=None)]

        try:
            queryset = [bookmark.sound for bookmark in Bookmark.objects.select_related("sound").filter(**kwargs)]
        except:
            raise NotFoundException

        get_analysis_data_for_queryset_or_sound_ids(self, queryset=queryset)
        #[bookmark.sound for bookmark in Bookmark.objects.select_related("sound").filter(user__username=self.kwargs['username'],category=None)]

        return queryset


############
# PACK VIEWS
############

class PackInstance(RetrieveAPIView):
    """
    Detailed pack information.
    TODO: proper doccumentation.
    """
    serializer_class = PackSerializer
    queryset = Pack.objects.all()

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        return super(PackInstance, self).get(request, *args, **kwargs)


class PackSounds(ListAPIView):
    """
    List of sounds in a pack.
    TODO: proper doccumentation.
    """
    serializer_class = SoundListSerializer

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        return super(PackSounds, self).get(request, *args, **kwargs)

    def get_queryset(self):
        try:
            Pack.objects.get(id=self.kwargs['pk'])
        except Pack.DoesNotExist:
            raise NotFoundException

        queryset = Sound.objects.select_related('pack').filter(moderation_state="OK",
                                                               processing_state="OK",
                                                               pack__id=self.kwargs['pk'])
        get_analysis_data_for_queryset_or_sound_ids(self, queryset=queryset)
        return queryset


class DownloadPack(DownloadAPIView):
    """
    Download a pack
    """

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        pack_id = kwargs['pk']
        try:
            pack = Pack.objects.get(id=pack_id)
        except Pack.DoesNotExist:
            raise NotFoundException

        sounds = pack.sound_set.filter(processing_state="OK", moderation_state="OK")
        if not sounds:
            raise NotFoundException(msg='Sounds in pack %i have not yet been described or moderated' % int(pack_id))

        try:
            filelist = "%s %i %s %s\r\n" % (pack.license_crc,
                                            os.stat(pack.locations('license_path')).st_size,
                                            pack.locations('license_url'),
                                            "_readme_and_license.txt")
        except:
            raise ServerErrorException

        for sound in sounds:
            url = sound.locations("sendfile_url")
            name = sound.friendly_filename()
            if sound.crc == '':
                continue
            filelist += "%s %i %s %s\r\n" % (sound.crc, sound.filesize, url, name)
        response = HttpResponse(filelist, content_type="text/plain")
        response['X-Archive-Files'] = 'zip'
        return response


##################
# READ WRITE VIEWS
##################

class UploadSound(WriteRequiredGenericAPIView):
    """
    Upload a sound (without description)
    TODO: proper doccumentation.
    """
    serializer_class = UploadAudioFileSerializer

    def post(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        serializer = UploadAudioFileSerializer(data=request.DATA, files=request.FILES)
        if serializer.is_valid():
            audiofile = request.FILES['audiofile']
            try:
                handle_uploaded_file(self.user.id, audiofile)
            except:
                raise ServerErrorException

            return Response(data={'filename': audiofile.name, 'details': 'File successfully uploaded (%i)' % audiofile.size}, status=status.HTTP_201_CREATED)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class NotYetDescribedUploadedSounds(WriteRequiredGenericAPIView):
    """
    List uploaded audio files which have not been yes described
    TODO: proper doccumentation.
    """

    def get(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        file_structure, files = generate_tree(os.path.join(settings.UPLOADS_PATH, str(self.user.id)))
        filenames = [file_instance.name for file_id, file_instance in files.items()]
        return Response(data={'filenames': filenames}, status=status.HTTP_200_OK)


class DescribeSound(WriteRequiredGenericAPIView):
    """
    Describe a previously uploaded audio file (audio file is identified by its filename which must be a post param)
    TODO: proper doccumentation.
    """
    serializer_class = SoundDescriptionSerializer

    def post(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        file_structure, files = generate_tree(os.path.join(settings.UPLOADS_PATH, str(self.user.id)))
        filenames = [file_instance.name for file_id, file_instance in files.items()]
        serializer = SoundDescriptionSerializer(data=request.DATA, context={'not_yet_described_audio_files': filenames})
        if serializer.is_valid():
            sound = create_sound_object(self.user, request.DATA)
            return Response(data={'details': 'Sound successfully described', 'uri': prepend_base(reverse('apiv2-sound-instance', args=[sound.id]))}, status=status.HTTP_201_CREATED)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UploadAndDescribeSound(WriteRequiredGenericAPIView):
    """
    Upload a sound and describe it (without description)
    TODO: proper doccumentation.
    """
    serializer_class = UploadAndDescribeAudioFileSerializer

    def post(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        serializer = UploadAndDescribeAudioFileSerializer(data=request.DATA, files=request.FILES)
        if serializer.is_valid():
            audiofile = request.FILES['audiofile']
            try:
                handle_uploaded_file(self.user.id, audiofile)
            except:
                raise ServerErrorException
            request.DATA['upload_filename'] = request.FILES['audiofile'].name
            sound = create_sound_object(self.user, request.DATA)
            return Response(data={'details': 'Audio file successfully uploaded and described', 'uri': prepend_base(reverse('apiv2-sound-instance', args=[sound.id])) }, status=status.HTTP_201_CREATED)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CreateBookmark(WriteRequiredGenericAPIView):
    """
    Create a new bookmark category of a user.
    DATA: name
    TODO: proper documentation.
    """
    serializer_class = CreateBookmarkSerializer

    def post(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        serializer = CreateBookmarkSerializer(data=request.DATA)
        if serializer.is_valid():
            if ('category' in request.DATA):
                category = BookmarkCategory.objects.get_or_create(user=self.user, name=request.DATA['category'])
                bookmark = Bookmark(user=self.user, name=request.DATA['name'], sound_id=request.DATA['sound_id'], category=category[0])
            else:
                bookmark = Bookmark(user=self.user, name=request.DATA['name'], sound_id=request.DATA['sound_id'])
            bookmark.save()
            return Response(data={'details': 'Bookmark successfully created'}, status=status.HTTP_201_CREATED)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CreateRating(WriteRequiredGenericAPIView):
    """
    Create a new rating of a sound.
    DATA: name
    TODO: proper documentation.
    """
    serializer_class = CreateRatingSerializer

    def post(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        serializer = CreateRatingSerializer(data=request.DATA)
        if serializer.is_valid():
            rating = Rating.objects.create(user=self.user, object_id=request.DATA['sound_id'], content_type=ContentType.objects.get(id=20), rating=int(request.DATA['rating'])*2)
            return Response(data={'details': 'Rating successfully created'}, status=status.HTTP_201_CREATED)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CreateComment(WriteRequiredGenericAPIView):
    """
    Create a new comment of a sound.
    DATA: name
    TODO: proper documentation.
    """
    serializer_class = CreateCommentSerializer

    def post(self, request,  *args, **kwargs):
        logger.info("TODO: proper logging")
        serializer = CreateCommentSerializer(data=request.DATA)
        if serializer.is_valid():
            comment = Comment.objects.create(user=self.user, object_id=request.DATA['sound_id'], content_type=ContentType.objects.get(id=20), comment=request.DATA['comment'])
            if comment.content_type == ContentType.objects.get_for_model(Sound):
                sound = comment.content_object
                sound.num_comments = sound.num_comments + 1
                sound.save()
            return Response(data={'details': 'Comment successfully created'}, status=status.HTTP_201_CREATED)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


#############
# OTHER VIEWS
#############

### Me View
class Me(GenericAPIView):
    """
    Returns some information about the end-user logged into the api
    TODO: proper documentation.
    """
    authentication_classes = (OAuth2Authentication, SessionAuthentication)

    def get(self, request,  *args, **kwargs):
        if self.user:
            response_data = {
                             'username': self.user.username,
                             'email': self.user.email,
                             'date_joined': self.user.date_joined,
                             'about': self.user.profile.about,
                             'home_page': self.user.profile.home_page,
                             'num_sound': self.user.profile.num_sounds,
                             'num_posts': self.user.profile.num_posts

            }
            return Response(response_data, status=status.HTTP_200_OK)
        else:
            raise ServerErrorException

### Root view
@api_view(('GET',))
@authentication_classes([OAuth2Authentication, TokenAuthentication, SessionAuthentication])
def freesound_api_v2_resources(request, format=None):

    '''
    List of resources available in the Freesound API V2.
    <br>
    Note that urls containing elements in brackets (<>) should be replaced with the corresponding
    variables.
    <br>
    '''

    return Response([
        {'Search resources': {
            'Search': prepend_base(reverse('apiv2-sound-search')),
            'Combined Search': prepend_base(reverse('apiv2-sound-combined-search')),
            'Similarity search with target file': prepend_base(reverse('apiv2-similarity-file')),
        }},
        {'Sound resources': {
            'Sound instance': prepend_base(reverse('apiv2-sound-instance', args=[0]).replace('0', '<sound_id>')),
            'Sound ratings': prepend_base(reverse('apiv2-sound-ratings', args=[0]).replace('0', '<sound_id>')),
            'Sound comments': prepend_base(reverse('apiv2-sound-comments', args=[0]).replace('0', '<sound_id>')),
            'Sound analysis': prepend_base(reverse('apiv2-sound-analysis', args=[0]).replace('0', '<sound_id>')) + '<filter>',
            'Download sound': prepend_base(reverse('apiv2-sound-download', args=[0]).replace('0', '<sound_id>')),
            'Similar sounds': prepend_base(reverse('apiv2-similarity-sound', args=[0]).replace('0', '<sound_id>')),
            'Sounds from list of sound ids': prepend_base(reverse('apiv2-sound-list-from-ids')),

        }},
        {'User resources': {
            'User instance': prepend_base(reverse('apiv2-user-instance', args=['uname']).replace('uname', '<username>')),
            'User sounds': prepend_base(reverse('apiv2-user-sound-list', args=['uname']).replace('uname', '<username>')),
            'User packs': prepend_base(reverse('apiv2-user-packs', args=['uname']).replace('uname', '<username>')),
            'User bookmark categories': prepend_base(reverse('apiv2-user-bookmark-categories', args=['uname']).replace('uname', '<username>')),
            'User uncategorized sound bookmarks': prepend_base(reverse('apiv2-user-bookmark-uncategorized', args=['uname']).replace('uname', '<username>')),
            'User sounds for bookmark category': prepend_base(reverse('apiv2-user-bookmark-category-sounds', args=['uname', 0]).replace('0', '<category_id>').replace('uname', '<username>')),
        }},
        {'Pack resources': {
            'Pack instance': prepend_base(reverse('apiv2-pack-instance', args=[0]).replace('0', '<pack_id>')),
            'Pack sounds': prepend_base(reverse('apiv2-pack-sound-list', args=[0]).replace('0', '<pack_id>')),
            'Download pack': prepend_base(reverse('apiv2-pack-download', args=[0]).replace('0', '<pack_id>')),
        }},
        {'Create resources': {
            'Upload sound': prepend_base(reverse('apiv2-uploads-upload')),
            'Describe uploaded sound': prepend_base(reverse('apiv2-uploads-describe')),
            'Uploaded sounds pending description': prepend_base(reverse('apiv2-uploads-not-described')),
            'Upload and describe sound': prepend_base(reverse('apiv2-uploads-upload-and-describe')),
            'Create bookmark': prepend_base(reverse('apiv2-user-create-bookmark')),
            'Create rating': prepend_base(reverse('apiv2-user-create-rating')),
            'Create comment': prepend_base(reverse('apiv2-user-create-comment')),
        }},
        {'Other resources': {
            'Oauth2 Authorize': prepend_base(reverse('oauth2:capture')),
            'Oauth2 Access token': prepend_base(reverse('oauth2:access_token')),
            'Apply': prepend_base(reverse('apiv2-apply')),
            'Me (information about user authenticated using oauth)': prepend_base(reverse('apiv2-me')),

        }},
    ])

### View for returning "Invalid url" 400 responses
@api_view(['GET'])
@authentication_classes([OAuth2Authentication, TokenAuthentication, SessionAuthentication])
def return_invalid_url(request):
    raise InvalidUrlException


### View for applying for an apikey
@login_required
def create_apiv2_key(request):

    if request.method == 'POST':
        form = ApiV2ClientForm(request.POST)
        if form.is_valid():
            api_client = ApiV2Client()
            api_client.user = request.user
            api_client.description = form.cleaned_data['description']
            api_client.name = form.cleaned_data['name']
            api_client.url = form.cleaned_data['url']
            api_client.redirect_uri = form.cleaned_data['redirect_uri']
            api_client.accepted_tos = form.cleaned_data['accepted_tos']
            api_client.save()
            form = ApiV2ClientForm()
    else:
        form = ApiV2ClientForm()

    if settings.APIV2KEYS_ALLOWED_FOR_APIV1:
        user_credentials = list(request.user.apiv2_client.all()) + list(request.user.api_keys.all())
    else:
        user_credentials = request.user.apiv2_client.all()
    fs_callback_url = request.build_absolute_uri(reverse('permission-granted'))

    return render_to_response('api/apply_key_apiv2.html',
                              {'user': request.user,
                               'form': form,
                               'user_credentials': user_credentials,
                               'combined_apiv1_and_apiv2': settings.APIV2KEYS_ALLOWED_FOR_APIV1,
                               'fs_callback_url': fs_callback_url,
                               }, context_instance=RequestContext(request))


### View for editing client (works both for apiv2 and apiv1)
@login_required
def edit_api_credential(request, key):
    client = None
    try:
        client = ApiV2Client.objects.get(key=key)
    except ApiV2Client.DoesNotExist:
        pass

    try:
        client = ApiKey.objects.get(key=key)
    except ApiKey.DoesNotExist:
        pass

    if not client:
        raise Http404

    if request.method == 'POST':
        if client.version == 'V2':
            form = ApiV2ClientForm(request.POST)
            if form.is_valid():
                client.name = form.cleaned_data['name']
                client.url = form.cleaned_data['url']
                client.redirect_uri = form.cleaned_data['redirect_uri']
                client.description = form.cleaned_data['description']
                client.accepted_tos = form.cleaned_data['accepted_tos']
                client.save()
                messages.add_message(request, messages.INFO, "Credentials with name %s have been updated." % client.name)
                return HttpResponseRedirect(reverse("apiv2-apply"))
        elif client.version == 'V1':
            form = ApiKeyForm(request.POST)
            if form.is_valid():
                client.name = form.cleaned_data['name']
                client.url = form.cleaned_data['url']
                client.description = form.cleaned_data['description']
                client.accepted_tos = form.cleaned_data['accepted_tos']
                client.save()
                messages.add_message(request, messages.INFO, "Credentials with name %s have been updated." % client.name)
                return HttpResponseRedirect(reverse("apiv2-apply"))
    else:
        if client.version == 'V2':
            form = ApiV2ClientForm(initial={'name': client.name,
                                            'url': client.url,
                                            'redirect_uri': client.redirect_uri,
                                            'description': client.description,
                                            'accepted_tos': client.accepted_tos
                                            })
        elif client.version == 'V1':
            form = ApiKeyForm(initial={'name': client.name,
                                        'url': client.url,
                                        'description': client.description,
                                        'accepted_tos': client.accepted_tos
                                        })

    fs_callback_url = request.build_absolute_uri(reverse('permission-granted'))
    return render_to_response('api/edit_api_credential.html',
                              {'client': client,
                               'form': form,
                               'fs_callback_url': fs_callback_url,
                               }, context_instance=RequestContext(request))


### View for deleting api clients (works both for apiv2 and apiv1)
@login_required
def delete_api_credential(request, key):
    name = ""

    try:
        client = ApiV2Client.objects.get(key=key)
        name = client.name
        client.delete()
    except ApiV2Client.DoesNotExist:
        pass

    try:
        client = ApiKey.objects.get(key=key)
        name = client.name
        client.delete()
    except ApiKey.DoesNotExist:
        pass

    messages.add_message(request, messages.INFO, "Credentials with name %s have been deleted." % name)
    return HttpResponseRedirect(reverse("apiv2-apply"))


### View for managing permissions granted to apps
@login_required
def granted_permissions(request):
    user = request.user
    tokens_raw = AccessToken.objects.select_related('client').filter(user=user).order_by('-expires')
    tokens = []
    token_names = []

    # If settings.OAUTH_SINGLE_ACCESS_TOKEN is set to false it is possible that one single user have more than one active
    # access token per application. In that case we only show the one that expires later (and all are removed if permissions
    # revoked). If settings.OAUTH_SINGLE_ACCESS_TOKEN is set to true we don't need the token name check below because
    # there can only be one access token per client-user pair. Nevertheless the code below works in both cases.

    for token in tokens_raw:
        if not token.client.apiv2_client.name in token_names:
            tokens.append({
                'client_name': token.client.apiv2_client.name,
                'expiration_date': token.expires,
                'expired': (token.expires - datetime.datetime.today()).total_seconds() < 0,
                'scope': token.client.apiv2_client.get_scope_display,
                'client_id': token.client.apiv2_client.client_id,
            })
        token_names.append(token.client.apiv2_client.name)

    return render_to_response('api/manage_permissions.html',
                              {'user': request.user, 'tokens': tokens},
                              context_instance=RequestContext(request))


### View to revoke permissions granted to an application
@login_required
def revoke_permission(request, client_id):
    user = request.user
    tokens = AccessToken.objects.filter(user=user, client__client_id=client_id)
    for token in tokens:
        token.delete()

    return HttpResponseRedirect(reverse("access-tokens"))


### View to show grant code (pin code) if application does not support redirection
@login_required
def permission_granted(request):
    user = request.user
    code = request.GET.get('code', None)
    app_name = None
    try:
        grant = Grant.objects.get(user=user, code=code)
        app_name = grant.client.apiv2_client.name
    except:
        grant = None

    if settings.USE_MINIMAL_TEMPLATES_FOR_OAUTH:
        template = 'api/minimal_app_authorized.html'
    else:
        template = 'api/app_authorized.html'

    return render_to_response(template,
                              {'code': code, 'app_name': app_name},
                              context_instance=RequestContext(request))


