# -*- coding: utf-8 -*-
# Generated by Django 1.9.5 on 2016-10-18 16:40
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_auto_20160914_1100'),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='is_adult',
            field=models.BooleanField(default=True),
        ),
    ]
