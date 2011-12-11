# -*- coding: utf-8 -*-
from django.contrib import admin
from ratings.models import Rating

class RatingAdmin(admin.ModelAdmin):
    raw_id_fields = ('user',)
    list_display = ('user', 'rating', 'created')
    search_fields = ('=user__username', )
    list_filter = ('rating',)

admin.site.register(Rating, RatingAdmin)