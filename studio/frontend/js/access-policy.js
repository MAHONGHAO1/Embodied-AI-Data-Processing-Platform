/** QuicStudio pure function access policy projection module, responsible for frontend view visibility and role label mappings. */
const QuicDataAccessPolicy = (() => {
  // Navigation views supported by the console (top-level and sub-navigation)
  const NAVIGATION_VIEWS = [
    'overview',
    'intake',
    'batches',
    'work-queue',
    'resources',
    'assets',
    'datasets',
    'trainDash',
    'trainJobs',
    'trainNew',
    'trainDatasets',
    'trainModels',
    'trainResources',
    'trainSystem',
    'admin',
    'settings',
  ];

  // Minimum required permission rules for each view
  const VIEW_RULES = {
    overview: ['workspace:read'],
    intake: ['batch:read', 'import:read'],
    batches: ['batch:read'],
    'work-queue': ['episode:read'],
    workbench: ['episode:read'],
    'package-workbench': ['episode:read'],
    'intake-review': ['episode:read'],
    resources: ['workspace:read', 'episode:read'],
    assets: ['dataset:read'],
    datasets: ['dataset:read'],
    trainDash: ['train:read'],
    trainJobs: ['train:read'],
    trainNew: ['train:read'],
    trainDatasets: ['train:read'],
    trainModels: ['train:read'],
    trainResources: ['train:read'],
    trainSystem: ['train:read'],
    miningTasks: ['batch:read'],
    miningDash: ['episode:read'],
    admin: ['*'],
    settings: ['*'],
  };

  // Permission items granting operational write access
  const OPERATIONAL_WRITE_PERMISSIONS = [
    'workspace:write',
    'batch:write',
    'import:write',
    'episode:write',
    'dataset:write',
  ];

  // Platform role localization dictionary
  const ROLE_LABELS = {
    admin: { 'zh-CN': '管理员', 'en-US': 'Administrator' },
    operator: { 'zh-CN': '数据运维', 'en-US': 'Data Operator' },
    annotator: { 'zh-CN': '标注员', 'en-US': 'Annotator' },
    auditor: { 'zh-CN': '审核员', 'en-US': 'Reviewer' },
    viewer: { 'zh-CN': '查看者', 'en-US': 'Viewer' },
  };

  /** Returns array of permission keys possessed by user */
  function permissionsOf(user) {
    return Array.isArray(user?.permissions)
      ? user.permissions.filter((permission) => typeof permission === 'string')
      : [];
  }

  /** Checks if user has specified permission (supports wildcards `*` and `scope:*`) */
  function hasPermission(user, permission) {
    if (typeof permission !== 'string' || !permission) return false;
    const permissions = permissionsOf(user);
    if (permissions.includes('*') || permissions.includes(permission)) return true;
    const separator = permission.indexOf(':');
    if (separator <= 0) return false;
    return permissions.includes(`${permission.slice(0, separator)}:*`);
  }

  /** Checks if user has permission to view specific view */
  function canView(user, view) {
    // The current training bridge requires write access even for its read APIs.
    if (view.startsWith('train') && !hasPermission(user, 'train:write')) return false;
    // Collection APIs require an admin and explicit workspace membership.
    // Workspace options expose the latter capability for scope selection.
    if (['intake', 'batches', 'intake-review', 'miningTasks', 'miningDash'].includes(view) && user?.role !== 'admin') return false;
    if (['work-queue', 'workbench', 'package-workbench'].includes(view)
      && !hasPermission(user, 'episode:annotate') && !hasPermission(user, 'episode:review')) return false;
    const required = VIEW_RULES[view];
    return Array.isArray(required) && required.every((permission) => hasPermission(user, permission));
  }

  /** Filters all navigation views accessible to user */
  function visibleViews(user) {
    return NAVIGATION_VIEWS.filter((view) => canView(user, view));
  }

  /** Checks if user has operational write permissions */
  function hasOperationalWrite(user) {
    return OPERATIONAL_WRITE_PERMISSIONS.some((permission) => hasPermission(user, permission));
  }

  /** Computes default landing view after user signs in */
  function defaultView(user) {
    const visible = visibleViews(user);
    if (!visible.length) return null;
    if (hasOperationalWrite(user) && canView(user, 'overview')) return 'overview';
    if (hasPermission(user, 'episode:annotate') && canView(user, 'work-queue')) return 'work-queue';
    if (hasPermission(user, 'episode:review') && canView(user, 'work-queue')) return 'work-queue';
    if (canView(user, 'overview')) return 'overview';
    return visible[0];
  }

  /** Computes default work stage (cut/annotation/review) when user enters work queue */
  function defaultQueueStage(user) {
    if (!canView(user, 'work-queue')) return null;
    if (hasPermission(user, 'episode:annotate') && !hasOperationalWrite(user)) return 'annotation';
    if (hasPermission(user, 'episode:review') && !hasPermission(user, 'episode:annotate')) return 'review';
    return 'annotation';
  }

  /** Gets localized display name for a role */
  function roleLabel(roleId, locale, fallbackLabel) {
    const normalizedRole = typeof roleId === 'string' ? roleId.trim() : '';
    const normalizedLocale = locale === 'en-US' ? 'en-US' : 'zh-CN';
    const stable = ROLE_LABELS[normalizedRole]?.[normalizedLocale];
    if (stable) return stable;
    if (typeof fallbackLabel === 'string' && fallbackLabel.trim()) return fallbackLabel.trim();
    return normalizedRole;
  }

  return {
    hasPermission,
    canView,
    visibleViews,
    defaultView,
    defaultQueueStage,
    roleLabel,
  };
})();
