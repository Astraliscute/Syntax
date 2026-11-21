(async () => {
  const assertions = [];
  const check = (condition, name) => {
    if (!condition) throw new Error(name);
    assertions.push(name);
  };
  const oldBridge = window.pywebview;
  clearTimeout(pollTimer);
  if (app.polling) await app.polling;
  const rows = Array.from({ length: 5000 }, (_, index) => ({
    id: index.toString(16).padStart(32, '0'), name: `DFIntExample${index}`,
    prefix: 'DFInt', suffix: `Example${index}`, type: 'int', value: String(index),
    enabled: true, missing: false, duplicate: false,
  }));
  const model = {
    settings: { rpc: true, motion: true, snow: true, theme: 'purple', tray: true, startup: true, offset_source: 'theo', LockF: true },
    calls: {}, preferenceActive: 0, preferenceMaximum: 0, update: { state: 'current' }, startup: { state: 'checking', message: 'Checking updates', completed: 2, total: 4, update_state: 'pending' },
  };
  const presets = [
    { id: 'sample', name: 'Sample', builtin: false, flags: { DFIntExample0: '0' }, controls: { DFIntExample0: { type: 'dropdown', label: 'Example value', options: { Zero: '0', One: '1' } } }, rows: rows.slice(0, 1) },
    { id: 'integer-sample', name: 'Integer sample', builtin: false, flags: { DFIntExample1: '1' }, controls: { DFIntExample1: { type: 'integer', label: 'Example value' } }, rows: rows.slice(1, 2) },
    { id: 'scc-sample', name: 'Built-in sample', builtin: true, flags: { DFIntExample2: '2' }, controls: {}, rows: rows.slice(2, 3) },
    { id: 'black-box', name: 'Black Box JSON Name', builtin: true, flags: { DFIntExample3: '3' }, controls: {}, rows: rows.slice(3, 4) },
  ];
  const packs = [{ id: 'sample', name: 'Large pack', description: 'UI regression fixture', rows }];
  const bridge = {
    get_state: async (after, revision, catalogRevision, statusRevision) => ({
      ok: true, revision: 1, catalog_revision: 1, preset_revision: 1,
      rows: revision === 1 && catalogRevision === 1 ? null : rows,
      status: statusRevision === 1 ? null : { state: 'waiting', revision: 1, flags: {} },
      settings: { ...model.settings }, update: { ...model.update }, startup: { ...model.startup },
      dump: { state: 'idle' }, configs: { state: 'bundled' }, logs: [], window_active: true,
    }),
    get_presets: async () => ({ ok: true, items: presets, revision: 1, catalog_revision: 1 }),
    get_community: async () => {
      model.calls.community = (model.calls.community || 0) + 1;
      return { ok: true, items: packs, revision: 1, catalog_revision: 1 };
    },
    get_catalog: async (query = '', page = 0, limit = 60) => {
      const matched = rows.filter(row => row.name.includes(query));
      return { ok: true, items: matched.slice(page * limit, (page + 1) * limit).map(row => ({
        name: row.name, full_name: row.name, clean: row.suffix, type: row.type, value: row.value,
      })), count: rows.length, total: matched.length, page, revision: 1, more: (page + 1) * limit < matched.length };
    },
    apply_flags: async () => {
      model.calls.apply = (model.calls.apply || 0) + 1;
      return { ok: true, queued: true };
    },
    complete_startup: async () => {
      model.startup = { state: 'ready', message: 'Ready', completed: 4, total: 4 };
      return { ok: true };
    },
    install_update: async () => {
      model.calls.install = (model.calls.install || 0) + 1;
      model.update = { ...model.update, state: 'downloading', received: 50, size: 100 };
      return { ok: true };
    },
    apply_preset: async (id, overrides) => {
      model.calls.preset = { id, overrides };
      return { ok: true };
    },
    apply_community: async id => {
      model.calls.pack = id;
      return { ok: true };
    },
    save_preferences: async (rpc, motion, snow, theme, tray, startup, LockF) => {
      model.preferenceActive++;
      model.preferenceMaximum = Math.max(model.preferenceMaximum, model.preferenceActive);
      await Promise.resolve();
      Object.assign(model.settings, { rpc, motion, snow, theme, tray, startup, LockF });
      model.preferenceActive--;
      return { ok: true };
    },
    inspect_flags: async () => ({ ok: true, rows: rows.slice(0, 1) }),
  };
  window.pywebview = { api: bridge };
  try {
    app.revision = app.catalogRevision = app.presetRevision = app.libraryCatalogRevision = -1;
    app.loaded = true;
    app.page = 'flags';
    await poll();
    check(!document.getElementById('startup-screen').hidden && document.getElementById('workspace-shell').hidden, 'loading hides the main workspace');
    check(document.getElementById('startup-message').textContent === 'Checking updates', 'loading reports the real check');
    model.startup = { state: 'update_available', message: 'Update available', completed: 3, total: 4, update_state: 'available' };
    model.update = { state: 'available', latest: 'v3.0.0', asset: 'Syntax.exe', download: 'https://github.com/Astraliscute/Syntax/releases/download/v3.0.0/Syntax.exe', url: 'https://github.com/Astraliscute/Syntax/releases/tag/v3.0.0' };
    await poll(true);
    check(!document.getElementById('startup-update').classList.contains('hidden') && !document.getElementById('startup-continue').classList.contains('hidden'), 'loading offers update or continue');
    await actions['startup-update']();
    check(document.getElementById('startup-progress').value === 50 && document.getElementById('startup-progress').max === 100, 'loading shows real download progress');
    check(document.getElementById('workspace-shell').hidden && document.getElementById('startup-continue').classList.contains('hidden'), 'download keeps startup controls gated');
    model.update = { state: 'error', error: 'Download failed' };
    await poll(true);
    await actions['startup-continue']();
    check(app.ready && document.getElementById('startup-screen').hidden && !document.getElementById('workspace-shell').hidden, 'continue opens the workspace');
    check(document.getElementById('desktop').getAttribute('aria-busy') === 'false' && !document.getElementById('workspace-shell').inert, 'startup releases keyboard access');
    model.update = { state: 'current' };
    await poll(true);
    render();
    await fetchCatalog();
    check(document.querySelectorAll('#flag-rows tr').length === 100, '5,000 flags render only first 100 rows');
    check(document.querySelectorAll('.catalog-row').length < 45, 'catalog DOM stays bounded');
    check(document.title === 'Syntax v2.0', 'version is 2.0');
    check(document.querySelector('[data-action="apply"]')?.textContent.trim() === 'Apply', 'Apply button is present');
    check(document.querySelector('[data-preference="LockF"]')?.getAttribute('aria-label') === 'Lock FFlags', 'Lock FFlags label is correct');
    check(document.querySelector('[data-action="inject"]') === null, 'Inject control is absent');
    await actions.apply();
    check(model.calls.apply === 1, 'Apply queues one request');
    await savePreferences({ LockF: false });
    check(model.settings.LockF === false, 'manual mode persists');
    await savePreferences({ LockF: true });
    check(model.settings.LockF === true, 'LockF mode persists');

    const input = document.querySelector('input[data-value]');
    input.focus();
    input.value = 'unfinished edit';
    input.classList.add('dirty');
    app.revision = -1;
    await poll();
    check(document.activeElement === input && input.value === 'unfinished edit', 'poll preserves active edit');
    input.blur();
    await poll();

    app.page = 'presets';
    await refreshPresets();
    check(document.querySelectorAll('[data-preset]').length === 2, 'saved presets retain their own list');
    check(document.querySelectorAll('[data-preset] [data-control]').length === 2, 'preset controls are retained');
    const dropdown = document.querySelector('select[data-control]');
    dropdown.value = '1';
    document.querySelector('[data-apply="sample"]').click();
    await poll(true);
    check(model.calls.preset?.overrides.DFIntExample0 === '1', 'preset dropdown reaches Apply');
    document.querySelector('input[data-control]').value = '42';
    document.querySelector('[data-apply="integer-sample"]').click();
    await poll(true);
    check(model.calls.preset?.overrides.DFIntExample1 === '42', 'preset integer reaches Apply');
    check(document.querySelector('[data-preset-debloat]') !== null, 'preset Debloat is retained');
    switchPage('scc');
    await refreshPresets();
    check(document.querySelectorAll('[data-preset]').length === 2, 'all JSON presets share their tab');
    check([...document.querySelectorAll('[data-preset] h3')].map(node => node.textContent).join('|') === presets.filter(preset => preset.builtin).map(preset => preset.name).join('|'), 'preset names exactly match JSON');
    check(document.querySelector('[data-community]') === null, 'repository presets exclude community actions');

    app.page = 'community';
    await refreshCommunity();
    const card = document.querySelector('.presetcard');
    const calls = model.calls.community;
    const started = performance.now();
    for (let index = 0; index < 30; index++) await poll();
    const idlePollMs = performance.now() - started;
    check(model.calls.community === calls, 'unchanged community is not fetched again');
    check(document.querySelector('.presetcard') === card, 'unchanged community DOM is retained');
    check(document.querySelectorAll('.preset-fflag').length === 5, 'large pack preview limited to five rows');
    check(!content.textContent.includes(packs[0].description), 'community descriptions are omitted');
    check(document.querySelector('[data-preset]') === null, 'community excludes repository entries');
    document.querySelector('[data-community="sample"]').click();
    await poll(true);
    check(model.calls.pack === 'sample', 'community pack Apply is retained');
    switchPage('settings');
    check(['snow', 'motion', 'rpc', 'tray', 'startup'].every(key => document.querySelector(`[data-preference="${key}"]`)), 'all settings switches are retained');
    check(document.querySelector('#theme-select')?.options.length === 7, 'seven theme choices are available');
    check(document.querySelector('#theme-select option[value="dark"]')?.textContent === 'Dark', 'Dark theme is listed');
    check(['tray', 'startup'].every(key => document.querySelector(`[data-preference="${key}"]`).getAttribute('aria-checked') === 'true'), 'tray and startup default on');
    check(document.querySelector('#offset-source')?.options.length === 2, 'both offset sources are retained');
    check(['dump-offsets', 'dump-cancel', 'sync', 'validate', 'updates', 'configs-refresh'].every(key => document.querySelector(`[data-action="${key}"]`)), 'offset and update actions are retained');
    check(document.getElementById('update-install').classList.contains('hidden'), 'no update button when current');
    model.update = { state: 'available', latest: 'v3.0.0', asset: 'Syntax.exe', download: 'https://github.com/Astraliscute/Syntax/releases/download/v3.0.0/Syntax.exe', url: 'https://github.com/Astraliscute/Syntax/releases/tag/v3.0.0' };
    await poll(true);
    check(!document.getElementById('update-install').classList.contains('hidden'), 'Syntax.exe update can be installed');
    const installed = model.calls.install || 0;
    await actions['update-install']();
    check(model.calls.install === installed + 1 && document.getElementById('release-status').textContent.includes('50%'), 'update download progress is displayed');
    model.update = { state: 'missing_asset', latest: 'Release1.1', error: 'Syntax.exe missing', url: 'https://github.com/Astraliscute/Syntax/releases/tag/Release1.1' };
    await poll(true);
    check(document.getElementById('update-install').classList.contains('hidden') && document.getElementById('release-status').textContent.includes('Syntax.exe missing'), 'missing asset cannot be installed');
    model.update = { state: 'current' };
    await poll(true);
    switchPage('credits');
    check(content.textContent.includes('wjl7') && content.textContent.includes('offp001'), 'contributor credits are retained');
    check([...document.querySelectorAll('.credit')].some(row => row.querySelector('strong')?.textContent === 'Syscall' && row.querySelector('small')?.textContent === 'Local Dumper'), 'Syscall has Local Dumper credit');
    const discordIcons = [...document.querySelectorAll('[data-action="discord"] svg')];
    check(discordIcons.length === 2 && discordIcons.every(svg => svg.classList.contains('discord-icon') && getComputedStyle(svg).stroke === 'none'), 'Discord uses filled brand icons');
    check(document.querySelector('[data-page="community"] svg path').getAttribute('d') !== paths.discord, 'community has a separate icon');
    check([...document.querySelectorAll('.titlelogo, .brand img, .about img')].every(image => image.src === window.syntaxLogos.purple), 'all app logos follow the theme');
    switchPage('logs');
    check(document.querySelector('[data-action="copy-logs"]') && document.querySelector('[data-action="clear-logs"]'), 'log actions are retained');

    await Promise.all([savePreferences({ snow: false }), savePreferences({ motion: false }), savePreferences({ theme: 'ocean' })]);
    check(!model.settings.snow && !model.settings.motion && model.settings.theme === 'ocean', 'simultaneous preference changes all persist');
    check(model.preferenceMaximum === 1, 'preference writes are serialized');

    newPresetModal();
    app.draft.rows = rows;
    renderDraftEditor();
    check(app.draft.mode === 'json' && document.querySelectorAll('.draft-row').length === 0, 'large draft uses JSON without thousands of inputs');
    closeModal();

    newPresetModal();
    document.getElementById('draft-new-name').value = 'DFIntExample0';
    document.getElementById('draft-new-value').value = '2';
    let finishInspection;
    bridge.inspect_flags = () => new Promise(resolve => { finishInspection = resolve; });
    const pendingAdd = addDraftRow();
    closeModal();
    finishInspection({ ok: true, rows: rows.slice(0, 1) });
    await pendingAdd;
    check(app.draft === null, 'closed draft ignores delayed validation');

    newPresetModal();
    let finishValidation, validationCalls = 0;
    bridge.inspect_flags = () => {
      validationCalls++;
      return new Promise(resolve => { finishValidation = resolve; });
    };
    const validations = [validateDraft(), validateDraft(), validateDraft()];
    check(validationCalls === 1, 'draft validation has only one in-flight bridge request');
    closeModal();
    finishValidation({ ok: true, rows: rows.slice(0, 1) });
    await Promise.all(validations);

    let finishSave;
    showModal('Pending save', '', () => new Promise(resolve => { finishSave = resolve; }));
    const pendingSave = saveModal();
    closeModal();
    showModal('Current modal', '', async () => ({ ok: true }));
    finishSave({ ok: true });
    await pendingSave;
    check(document.getElementById('modal-title').textContent === 'Current modal', 'old save response does not close new modal');
    closeModal();

    const themeColors = new Set();
    const logoSources = new Set();
    check(Object.keys(window.syntaxLogos).length === 7, 'seven logo variants are embedded');
    for (const theme of ['purple', 'dark', 'midnight', 'ocean', 'forest', 'rose', 'amber']) {
      app.settings.theme = theme;
      updateEffects();
      check((document.body.dataset.theme || 'purple') === theme, `theme ${theme} applies`);
      themeColors.add(getComputedStyle(document.body).backgroundColor);
      const images = [...document.querySelectorAll('.titlelogo, .brand img, .about img')];
      await Promise.all(images.map(image => image.complete && image.naturalWidth ? Promise.resolve() : new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('Logo load timed out')), 5000);
        image.addEventListener('load', () => { clearTimeout(timer); resolve(); }, { once: true });
        image.addEventListener('error', () => { clearTimeout(timer); reject(new Error('Logo load failed')); }, { once: true });
      })));
      check(images.every(image => image.src === window.syntaxLogos[theme] && image.dataset.logoTheme === theme), `logo follows ${theme}`);
      logoSources.add(images[0].src);
    }
    check(themeColors.size === 7, 'all theme palettes remain distinct');
    check(logoSources.size === 7, 'all logo palettes remain distinct');
    app.settings.theme = 'purple';
    updateEffects();
    for (let index = 0; index < 20; index++) toast('Regression fixture');
    check(document.querySelectorAll('.toast').length === 5, 'toast queue is bounded');
    document.getElementById('toasts').replaceChildren();
    switchPage('flags');
    return { passed: assertions.length, assertions, flags: rows.length,
      idlePollMs: Math.round(idlePollMs * 100) / 100,
      domElements: document.querySelectorAll('*').length,
      renderedCatalogRows: document.querySelectorAll('.catalog-row').length };
  } finally {
    clearTimeout(pollTimer);
    window.pywebview = oldBridge;
  }
})();
