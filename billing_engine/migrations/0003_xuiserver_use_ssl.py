from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing_engine', '0002_vpnplan_proxysubscription_transaction_and_more'),
    ]

    operations = [
        # Use RunSQL so SQLite stores an actual DEFAULT 0 in the column definition,
        # which prevents NOT NULL constraint failures when the ORM inserts a row
        # without explicitly supplying the value.
        migrations.RunSQL(
            sql="ALTER TABLE billing_engine_xuiserver ADD COLUMN use_ssl bool NOT NULL DEFAULT 0;",
            reverse_sql="SELECT 1;",  # SQLite cannot DROP COLUMN in older versions; no-op on rollback
        ),
        # Tell Django's migration state about the field so it stops trying to manage it
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='xuiserver',
                    name='use_ssl',
                    field=models.BooleanField(
                        default=False,
                        help_text='Enable if your 3x-ui panel runs on HTTPS (self-signed certs are accepted).'
                    ),
                ),
            ],
            database_operations=[],  # DB change already done above via RunSQL
        ),
    ]
