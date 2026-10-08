/** Declarative registry for workbench keyboard shortcuts, providing chord mappings, stage scope filtering, and help modal definitions. */
const QuicDataWorkbenchShortcuts = (() => {
  const COMMANDS = Object.freeze([
    { id: 'help-toggle', chord: '?', labelKey: 'shortcutHelp', scope: 'all', triggers: [{ key: '?' }] },
    { id: 'help-close', chord: 'Esc', labelKey: 'shortcutClose', scope: 'all', triggers: [{ key: 'Escape' }] },
    { id: 'history-undo', chord: 'Cmd/Ctrl+Z', labelKey: 'undo', scope: 'all', triggers: [{ key: 'z', command: true }] },
    { id: 'history-redo', chord: 'Cmd/Ctrl+Shift+Z', labelKey: 'redo', scope: 'all', triggers: [{ key: 'z', command: true, shift: true }, { key: 'y', command: true }] },
    { id: 'annotation-copy', chord: 'Cmd/Ctrl+C', labelKey: 'copy', scope: 'annotation', triggers: [{ key: 'c', command: true }] },
    { id: 'annotation-paste', chord: 'Cmd/Ctrl+V', labelKey: 'paste', scope: 'annotation', triggers: [{ key: 'v', command: true }] },
    { id: 'play-toggle', chord: 'Space', labelKey: 'shortcutPlayPause', scope: 'all', triggers: [{ key: ' ' }] },
    { id: 'step-backward', chord: 'Left', labelKey: 'stepBackward', scope: 'all', triggers: [{ key: 'ArrowLeft' }] },
    { id: 'step-forward', chord: 'Right', labelKey: 'stepForward', scope: 'all', triggers: [{ key: 'ArrowRight' }] },
    { id: 'step-backward-large', chord: 'Shift+Left', labelKey: 'stepBackwardLarge', scope: 'all', triggers: [{ key: 'ArrowLeft', shift: true }] },
    { id: 'step-forward-large', chord: 'Shift+Right', labelKey: 'stepForwardLarge', scope: 'all', triggers: [{ key: 'ArrowRight', shift: true }] },
    { id: 'shuttle-backward', chord: 'J', labelKey: 'shortcutShuttleBackward', scope: 'all', triggers: [{ key: 'j' }] },
    { id: 'shuttle-pause', chord: 'K', labelKey: 'shortcutShuttlePause', scope: 'all', triggers: [{ key: 'k' }] },
    { id: 'shuttle-forward', chord: 'L', labelKey: 'shortcutShuttleForward', scope: 'all', triggers: [{ key: 'l' }] },
    { id: 'boundary-previous', chord: '[', labelKey: 'shortcutPreviousBoundary', scope: 'all', triggers: [{ key: '[' }] },
    { id: 'boundary-next', chord: ']', labelKey: 'shortcutNextBoundary', scope: 'all', triggers: [{ key: ']' }] },
    { id: 'timeline-zoom-in', chord: '+', labelKey: 'zoomIn', scope: 'all', triggers: [{ key: '+' }, { key: '=' }] },
    { id: 'timeline-zoom-out', chord: '-', labelKey: 'zoomOut', scope: 'all', triggers: [{ key: '-' }, { key: '_' }] },
    { id: 'timeline-snap', chord: 'S', labelKey: 'shortcutSnap', scope: 'all', triggers: [{ key: 's' }] },
    { id: 'cut-select-previous', chord: 'Up', labelKey: 'shortcutSelectPrevious', scope: 'cut', triggers: [{ key: 'ArrowUp' }] },
    { id: 'cut-select-next', chord: 'Down', labelKey: 'shortcutSelectNext', scope: 'cut', triggers: [{ key: 'ArrowDown' }] },
    { id: 'cut-delete-boundary', chord: 'Delete', labelKey: 'deleteBoundary', scope: 'cut', triggers: [{ key: 'Delete' }] },
    { id: 'cut-add-boundary', chord: 'B', labelKey: 'addBoundary', scope: 'cut', triggers: [{ key: 'b' }] },
    { id: 'cut-restore-qr', chord: 'R', labelKey: 'restoreQrBoundary', scope: 'cut', triggers: [{ key: 'r' }] },
    { id: 'cut-include', chord: 'V', labelKey: 'segmentIncluded', scope: 'cut', triggers: [{ key: 'v' }] },
    { id: 'cut-exclude', chord: 'X', labelKey: 'segmentExcluded', scope: 'cut', triggers: [{ key: 'x' }] },
    { id: 'annotation-add-segment', chord: 'N', labelKey: 'addSegment', scope: 'annotation', triggers: [{ key: 'n' }] },
    { id: 'annotation-set-start', chord: 'I', labelKey: 'setStart', scope: 'annotation', triggers: [{ key: 'i' }] },
    { id: 'annotation-set-end', chord: 'O', labelKey: 'setEnd', scope: 'annotation', triggers: [{ key: 'o' }] },
  ]);

  function supports(command, capabilities) {
    if (command.scope === 'all') return true;
    return Boolean(capabilities?.[command.scope]);
  }

  function commandsFor(capabilities) {
    return COMMANDS.filter((command) => command.id !== 'help-close' && supports(command, capabilities));
  }

  function shouldIgnoreTarget(target) {
    const tagName = String(target?.tagName || '').toUpperCase();
    return Boolean(target?.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(tagName));
  }

  function triggerMatches(event, trigger) {
    const key = String(event?.key || '');
    const normalizedKey = key.length === 1 ? key.toLowerCase() : key;
    const expectedKey = trigger.key.length === 1 ? trigger.key.toLowerCase() : trigger.key;
    const command = Boolean(event?.ctrlKey || event?.metaKey);
    return normalizedKey === expectedKey
      && command === Boolean(trigger.command)
      && Boolean(event?.shiftKey) === Boolean(trigger.shift)
      && !event?.altKey;
  }

  function resolve(event, capabilities) {
    if (!event || event.isComposing || event.altKey) return null;
    return COMMANDS.find((command) => supports(command, capabilities)
      && command.triggers.some((trigger) => triggerMatches(event, trigger))) || null;
  }

  return Object.freeze({ COMMANDS, commandsFor, shouldIgnoreTarget, resolve });
})();

if (typeof globalThis !== 'undefined') globalThis.QuicDataWorkbenchShortcuts = QuicDataWorkbenchShortcuts;
