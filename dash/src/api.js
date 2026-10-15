import { t } from "./i18n.js";

export function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

export function changed(base, next) {
  if (JSON.stringify(base) === JSON.stringify(next)) return {};
  if (!base || !next || typeof base !== "object" || typeof next !== "object" || Array.isArray(base) || Array.isArray(next)) {
    return next;
  }
  const patch = {};
  for (const key of new Set([...Object.keys(base), ...Object.keys(next)])) {
    if (!(key in next)) patch[key] = null;
    else if (JSON.stringify(base[key]) !== JSON.stringify(next[key])) patch[key] = changed(base[key], next[key]);
  }
  return patch;
}

export function connectionFields(form) {
  const mode = form.type === "qq_official_v2_webhook" ? "manual" : form.shard_mode;
  const shard = mode === "auto" || form.type === "qq_official_v2_webhook" ? [0, 1] : [Number(form.shardIndex), Number(form.shardCount)];
  if (!Number.isInteger(shard[0]) || !Number.isInteger(shard[1]) || shard[0] < 0 || shard[1] < 1 || shard[0] >= shard[1]) {
    throw new Error(t("msg.invalidShard"));
  }
  return {
    appid: (form.appid || "").trim(),
    use_markdown: Boolean(form.use_markdown),
    type: form.type,
    intents: Number(form.intents),
    shard_mode: mode,
    shard,
    enable: Boolean(form.enable),
  };
}

export function createApi(state) {
  const post = (endpoint, body = {}) => state.bridge.apiPost(endpoint, { csrf: state.boot.csrf, ...body });
  const get = (endpoint, params = {}) => state.bridge.apiGet(endpoint, params);
  const needView = () => {
    if (!state.view) throw new Error(t("msg.readConnectionFirst"));
  };
  const needCurrent = () => {
    if (!state.current) throw new Error(t("msg.noLoadedConfig"));
  };

  return {
    get,
    post,
    async setFlag(name, value) {
      const response = await post("flags", {
        revision: state.boot.flags_revision,
        patch: { [name]: value },
        confirm: true,
      });
      state.boot.flags = response.flags;
      state.boot.flags_revision = response.revision;
      return response;
    },
    async saveConnection(payload) {
      needView();
      return post("connection/save", {
        platform_id: state.view.platform_id,
        fingerprint: state.view.fingerprint,
        ...payload,
      });
    },
    async reloadConnection() {
      needView();
      return post("connection/reload", {
        platform_id: state.view.platform_id,
        fingerprint: state.view.fingerprint,
        confirm: true,
      });
    },
    async startScan() {
      needView();
      if (["creating", "pending", "ready_to_commit"].includes(state.scan?.state) && state.scan?.ticket) {
        await post("onboarding/cancel", { platform_id: state.scan.platform_id, ticket: state.scan.ticket }).catch(() => {});
      }
      state.scan = await post("onboarding/start", {
        platform_id: state.view.platform_id,
        fingerprint: state.view.fingerprint,
        confirm: true,
      });
      return state.scan;
    },
    async refreshScan() {
      if (!state.scan?.ticket) return null;
      state.scan = await post("onboarding/status", {
        platform_id: state.scan.platform_id,
        ticket: state.scan.ticket,
        renew: true,
      });
      return state.scan;
    },
    async cancelScan() {
      if (!state.scan?.ticket) return null;
      state.scan = await post("onboarding/cancel", {
        platform_id: state.scan.platform_id,
        ticket: state.scan.ticket,
      });
      return state.scan;
    },
    async commitScan() {
      needView();
      return post("onboarding/commit", {
        platform_id: state.view.platform_id,
        ticket: state.scan.ticket,
        commit_handle: state.scan.commit_handle,
        fingerprint: state.view.fingerprint,
        confirm: true,
        confirm_secret: true,
        confirm_identity: true,
      });
    },
    async mutate(operation, patch, restoreRevision) {
      needCurrent();
      return post("config/mutate", {
        platform_id: state.current.platform_id,
        revision: state.current.revision,
        fingerprint: state.current.fingerprint,
        operation,
        ...(operation === "save" ? { patch } : {}),
        ...(operation === "restore" ? { restore_revision: restoreRevision } : {}),
        ...(operation !== "save" ? { confirm: true } : {}),
        ...(operation === "apply" ? { confirm_bindings: Boolean(state.confirmBindings) } : {}),
      });
    },
    async preview(payload) {
      needCurrent();
      return post("preview", {
        platform_id: state.current.platform_id,
        fingerprint: state.current.fingerprint,
        scene: state.scene,
        revision: state.current.revision,
        ...payload,
      });
    },
    async planPanels(options) {
      needCurrent();
      return post("panels/plan", { platform_id: state.platformId, fingerprint: state.current.fingerprint, scene: state.scene, ...options });
    },
    async enablePanels(options) {
      needCurrent();
      return post("panels/enable", { platform_id: state.platformId, fingerprint: state.current.fingerprint, scene: state.scene, confirm: true, ...options });
    },
    async syncPanels() {
      needCurrent();
      return post("panels/sync", { platform_id: state.platformId, fingerprint: state.current.fingerprint, scene: state.scene, confirm: true });
    },
    async disablePanels() {
      needCurrent();
      return post("panels/disable", { platform_id: state.platformId, fingerprint: state.current.fingerprint, scene: state.scene, confirm: true });
    },
    async retained() {
      if (!state.view) throw new Error(t("msg.readCurrentTargetFirst"));
      return get("inbox/retained", { platform_id: state.platformId });
    },
    async discardRetained(payload) {
      return post("inbox/discard", payload);
    },
  };
}
