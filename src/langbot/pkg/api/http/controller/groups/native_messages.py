"""Small localized API errors for native source administration."""
import quart

_MESSAGES = {
    'en_US': {
        'operation_unknown': 'Native source operation is not available',
        'original_unavailable': 'The current original is not available',
        'feed_conflict': 'Invalid cursor or pending acknowledgement',
        'ack_invalid': 'Exact page and receipts are required',
        'ack_conflict': 'Receipt does not match the frozen source page',
        'registration_invalid': 'Only registration settings are accepted',
    },
    'zh_Hans': {
        'operation_unknown': '此来源操作不可用',
        'original_unavailable': '当前版本的原件不可用',
        'feed_conflict': '游标无效，或上一页尚未确认',
        'ack_invalid': '需要提供准确的页标识和接纳回执',
        'ack_conflict': '回执与已冻结的来源页不一致',
        'registration_invalid': '仅接受指定的接入登记字段',
    },
}


def message(key):
    language = quart.request.accept_languages.best_match(['en-US','zh-Hans','zh-CN'],default='en-US')
    locale = 'zh_Hans' if language.startswith('zh') else 'en_US'
    return _MESSAGES[locale][key]
