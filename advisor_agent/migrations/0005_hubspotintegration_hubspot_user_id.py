# Generated by Django 5.0.2 on 2025-07-14 10:28

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('advisor_agent', '0004_remove_ongoinginstruction_updated_at_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='hubspotintegration',
            name='hubspot_user_id',
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
    ]
