export const nativeIngressEn = {
  title: 'Native source',
  routedBy:
    'Events are dispatched by the existing Router. Pipeline routing and plugin processors do not apply.',
  state: {
    source_available: 'Source reachable; upstream not verified',
    awaiting_upstream: 'Awaiting upstream connection',
    disabled: 'Disabled',
    needs_authorization: 'Authorization required',
    connecting: 'Connecting',
    connected: 'Connected',
    disconnected: 'Disconnected',
    error: 'Connection error',
    unknown: 'Not verified',
  },
  counts: {
    source_events: 'Source accepted',
    router_accepted: 'Router accepted',
    pending_host: 'Awaiting Router',
    quarantines: 'Quarantined',
    owner_gaps: 'Coverage gaps',
  },
  stage: {
    source_accepted: 'Source accepted',
    router_accepted: 'Router accepted',
    quarantined: 'Quarantined',
  },
  record: 'Native record',
  version: 'Version',
  receipt: 'Receipt',
  kind: 'Kind',
  empty: 'No accepted records to display.',
  more: 'Showing recent records only.',
  resultBoundary:
    'Router acceptance confirms handoff. Business execution and its result remain in the original OpenAgents thread.',
  authority:
    'Register the exact source and audience through the controlled service path. A credential reference alone grants no access.',
  sessions:
    'This source uses the existing Router and OpenAgents thread; it does not create a Pipeline chat session.',
};
export const nativeIngressZhHans = {
  title: '原生消息来源',
  routedBy:
    '由现有 Router 派发，普通 Pipeline 路由和插件处理器不适用于此来源。',
  state: {
    source_available: '来源可达，上游未核验',
    awaiting_upstream: '等待上游连接或采集',
    disabled: '已停用',
    needs_authorization: '需要来源授权',
    connecting: '连接中',
    connected: '已连接',
    disconnected: '连接已断开',
    error: '连接异常',
    unknown: '尚未核验',
  },
  counts: {
    source_events: '来源已接纳',
    router_accepted: 'Router 已接纳',
    pending_host: '等待 Router',
    quarantines: '已隔离',
    owner_gaps: '覆盖缺口',
  },
  stage: {
    source_accepted: '来源已接纳',
    router_accepted: 'Router 已接纳',
    quarantined: '已隔离',
  },
  record: '原生记录',
  version: '版本',
  receipt: '回执',
  kind: '类型',
  empty: '暂无可显示的已接纳记录。',
  more: '仅显示最近记录。',
  resultBoundary:
    'Router 接纳只证明已交接，业务执行及结果仍在 OpenAgents 原线程中查看。',
  authority:
    '请通过受控服务登记准确来源与受众，填写凭据引用本身不会获得访问权限。',
  sessions:
    '此来源使用现有 Router 和 OpenAgents 原线程，不创建 Pipeline 聊天会话。',
};
export const nativeIngressZhHant = {
  title: '原生訊息來源',
  routedBy:
    '由現有 Router 派發，普通 Pipeline 路由和外掛處理器不適用於此來源。',
  state: {
    source_available: '來源可達，上游未核驗',
    awaiting_upstream: '等待上游連線或採集',
    disabled: '已停用',
    needs_authorization: '需要來源授權',
    connecting: '連線中',
    connected: '已連線',
    disconnected: '連線已中斷',
    error: '連線異常',
    unknown: '尚未核驗',
  },
  counts: {
    source_events: '來源已接納',
    router_accepted: 'Router 已接納',
    pending_host: '等待 Router',
    quarantines: '已隔離',
    owner_gaps: '覆蓋缺口',
  },
  stage: {
    source_accepted: '來源已接納',
    router_accepted: 'Router 已接納',
    quarantined: '已隔離',
  },
  record: '原生記錄',
  version: '版本',
  receipt: '回執',
  kind: '類型',
  empty: '暫無可顯示的已接納記錄。',
  more: '僅顯示最近記錄。',
  resultBoundary:
    'Router 接納僅證明已交接，業務執行及結果仍在 OpenAgents 原執行緒中查看。',
  authority:
    '請透過受控服務登記準確來源與受眾，填寫憑據參照本身不會獲得存取權限。',
  sessions:
    '此來源使用現有 Router 和 OpenAgents 原執行緒，不建立 Pipeline 聊天工作階段。',
};
