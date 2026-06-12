from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing_engine', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='xuiserver',
            name='use_ssl',
            field=models.BooleanField(
                default=False,
                help_text='Enable if your 3x-ui panel runs on HTTPS (self-signed certs are accepted).'
            ),
        ),
    ]
