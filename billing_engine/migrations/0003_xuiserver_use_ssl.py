from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing_engine', '0002_vpnplan_proxysubscription_transaction_and_more'),
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
