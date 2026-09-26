export function createApi(state) {
  const scene = () => state.scene;
  function configPayload(extra = {}) {
    if (!state.current) throw new Error("请先读取这个机器人。");
    return { platform_id: state.current.platform_id, scene: scene(), revision: state.current.revision, fingerprint: state.current.fingerprint, csrf: state.boot.csrf, ...extra };
  }
  function connectionPayload(extra = {}) {
    if (!state.view || state.platformId.trim() !== state.view.platform_id) throw new Error("请先读取连接。");
    return { platform_id: state.view.platform_id, fingerprint: state.view.fingerprint, csrf: state.boot.csrf, ...extra };
  }
  return {
    scene,
    configPayload,
    connectionPayload,
    async loadCatalog() {
      state.catalog = await state.bridge.apiGet("commands", { platform_id: state.current.platform_id, scene: scene() });
      state.loadedScene = scene();
    },
    async saveConnection(body) {
      state.view = await state.bridge.apiPost("connection/save", connectionPayload(body));
    },
    async reloadConnection() {
      state.view = await state.bridge.apiPost("connection/reload", connectionPayload({ confirm: true }));
    },
    async startScan() {
      if (["creating", "pending", "ready_to_commit"].includes(state.scan?.state)) await state.bridge.apiPost("onboarding/cancel", connectionPayload({ ticket: state.scan.ticket }));
      state.scan = await state.bridge.apiPost("onboarding/start", connectionPayload({ confirm: true }));
    },
    async refreshScan() {
      state.scan = await state.bridge.apiPost("onboarding/status", connectionPayload({ ticket: state.scan.ticket, renew: true }));
    },
    async cancelScan() {
      if (state.scan) state.scan = await state.bridge.apiPost("onboarding/cancel", connectionPayload({ ticket: state.scan.ticket }));
    },
    async commitScan() {
      const saved = await state.bridge.apiPost("onboarding/commit", connectionPayload({ ticket: state.scan.ticket, commit_handle: state.scan.commit_handle, confirm: true, confirm_secret: true, confirm_identity: true }));
      state.scan = saved;
      if (saved.result) state.view = saved.result;
    },
    async mutate(operation, patch, restoreRevision) {
      await state.bridge.apiPost("config/mutate", configPayload({ operation, patch, confirm: operation === "apply", confirm_bindings: state.confirmBindings, restore_revision: restoreRevision }));
    },
    async preview(body) {
      return state.bridge.apiPost("preview", configPayload(body));
    },
    async planPanels(options) {
      return state.bridge.apiPost("panels/plan", configPayload(options));
    },
    async enablePanels(options) {
      return state.bridge.apiPost("panels/enable", configPayload({ ...options, confirm: true }));
    },
    async syncPanels() {
      return state.bridge.apiPost("panels/sync", configPayload({ confirm: true }));
    },
    async disablePanels() {
      return state.bridge.apiPost("panels/disable", configPayload({ confirm: true }));
    },
    async setFlag(name, enabled) {
      const value = await state.bridge.apiPost("flags", { csrf: state.boot.csrf, revision: state.boot.flags_revision, patch: { [name]: enabled }, confirm: true });
      state.boot.flags = value.flags;
      state.boot.flags_revision = value.revision;
    },
    async retained() {
      return state.bridge.apiGet("inbox/retained", { platform_id: state.current.platform_id });
    },
    async discardRetained(body) {
      return state.bridge.apiPost("inbox/discard", body);
    },
  };
}
