"""Protected metadata handoff to the existing single Router Host."""

import quart

from .. import group
from ...authz import Permission
from .....platform.native_runtime import authorized_native_store


@group.group_class('native_ingress', '/api/v1/native-ingress')
class NativeIngressRouterGroup(group.RouterGroup):
    async def initialize(self):
        @self.route('/<bot_uuid>/router/changes', methods=['GET'],
                    auth_type=group.AuthType.API_KEY, permission=Permission.RUNTIME_OPERATE)
        async def changes(bot_uuid, request_context):
            store, authorize, _ = await authorized_native_store(self.ap, request_context, bot_uuid)
            try:
                limit = int(quart.request.args.get('limit', '100'))
                page = await store.read_router_page(quart.request.args.get('cursor'), limit, authorize=authorize)
            except ValueError:
                return self.http_status(409, 'native_feed_conflict', 'Invalid cursor or pending acknowledgement')
            return self.success(data=page)

        @self.route('/<bot_uuid>/router/ack', methods=['POST'],
                    auth_type=group.AuthType.API_KEY, permission=Permission.RUNTIME_OPERATE)
        async def ack(bot_uuid, request_context):
            store, authorize, _ = await authorized_native_store(self.ap, request_context, bot_uuid)
            body = await quart.request.get_json()
            if not isinstance(body, dict) or set(body) != {'page_id', 'page_sha256', 'receipts'}:
                return self.http_status(400, 'native_ack_invalid', 'Exact page and receipts are required')
            try:
                result = await store.ack_router_page(**body, authorize=authorize)
            except ValueError:
                return self.http_status(409, 'native_ack_conflict', 'Receipt does not match the frozen source page')
            return self.success(data=result)

        @self.route('/<bot_uuid>/router/receipts/<event_id>', methods=['GET'],
                    auth_type=group.AuthType.API_KEY, permission=Permission.RUNTIME_OPERATE)
        async def lookup(bot_uuid, event_id, request_context):
            store, authorize, _ = await authorized_native_store(self.ap, request_context, bot_uuid)
            return self.success(data=await store.lookup_router_receipt(event_id, authorize=authorize))

        @self.route('/<bot_uuid>/registration', methods=['PUT'],
                    auth_type=group.AuthType.USER_TOKEN, permission=Permission.API_KEY_MANAGE)
        async def register(bot_uuid, request_context):
            from .....platform.native_access import register_native_access, registration_binding
            # The service additionally requires ACCOUNT and resource.manage;
            # support/global keys cannot register source audiences.
            body = await quart.request.get_json()
            allowed = {'receiver_id','receiver_api_key_uuid','source_enabled','receiver_enabled','expected_revision'}
            if not isinstance(body,dict) or not set(body) <= allowed:
                return self.http_status(400,'native_registration_invalid','Only registration settings are accepted')
            result = await register_native_access(self.ap,request_context,bot_uuid,
                await registration_binding(self.ap,request_context,bot_uuid),**body)
            return self.success(data=result)
