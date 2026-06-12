import json
from django.core.management.base import BaseCommand
from billing_engine.models import XuiServer, XuiInbound
from billing_engine.xui_client import XuiAPIClient


class Command(BaseCommand):
    help = 'Sync inbounds from all active 3x-ui servers into the database'

    def handle(self, *args, **options):
        servers = XuiServer.objects.filter(is_active=True)
        if not servers.exists():
            self.stdout.write(self.style.WARNING('No active servers found.'))
            return

        for server in servers:
            self.stdout.write(f'\nSyncing server: {server.name} ({server.get_host()})')
            try:
                client = XuiAPIClient(server)
                result = client.get_inbounds()
                inbounds = result.get('obj', [])
                created_count = 0

                for ib in inbounds:
                    try:
                        stream_raw = ib.get('streamSettings', '{}')
                        stream = json.loads(stream_raw) if isinstance(stream_raw, str) else stream_raw
                    except (json.JSONDecodeError, TypeError):
                        stream = {}

                    inbound, created = XuiInbound.objects.get_or_create(
                        server=server,
                        xui_inbound_id=ib['id'],
                        defaults={
                            'protocol': ib.get('protocol', 'VLESS').upper(),
                            'stream_settings': stream,
                            'is_available_for_purchase': True,
                        }
                    )
                    created_count += int(created)
                    status = 'CREATED' if created else 'EXISTS '
                    self.stdout.write(
                        f'  [{status}] id={ib["id"]:>3}  protocol={ib.get("protocol","?"):<12}  tag={ib.get("tag","?")}'  
                    )

                self.stdout.write(
                    self.style.SUCCESS(f'  Done: {created_count} new, {len(inbounds) - created_count} already existed.')
                )

            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  ERROR: {e}'))

        total = XuiInbound.objects.count()
        self.stdout.write(self.style.SUCCESS(f'\nTotal inbounds in DB: {total}'))
