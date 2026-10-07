export function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

// The settings API uses a recursive merge patch. A null value removes a nested field.
export function changed(oldValue = {}, nextValue = {}) {
  const patch = {};
  const keys = new Set([...Object.keys(oldValue || {}), ...Object.keys(nextValue || {})]);
  for (const key of keys) {
    if (!(key in nextValue)) {
      patch[key] = null;
      continue;
    }
    const oldItem = oldValue?.[key];
    const nextItem = nextValue[key];
    if (oldItem && nextItem && typeof oldItem === "object" && typeof nextItem === "object"
        && !Array.isArray(oldItem) && !Array.isArray(nextItem)) {
      const nested = changed(oldItem, nextItem);
      if (Object.keys(nested).length) patch[key] = nested;
    } else if (JSON.stringify(oldItem) !== JSON.stringify(nextItem)) {
      patch[key] = clone(nextItem);
    }
  }
  return patch;
}

export function connectionFields(form) {
  if (!form) throw new Error("请先读取连接配置。");
  const shard = [Number(form.shardIndex), Number(form.shardCount)];
  if (!Number.isInteger(shard[0]) || !Number.isInteger(shard[1]) || shard[0] < 0 || shard[1] < 1 || shard[0] >= shard[1]) {
    throw new Error("分片必须是有效的 [片号, 总片数]。");
  }
  if (form.type === "qq_official_v2_webhook" && (shard[0] !== 0 || shard[1] !== 1)) {
    throw new Error("Webhook 只能使用 [0, 1] 分片。");
  }
  return {
    appid: String(form.appid || "").trim(),
    use_markdown: Boolean(form.use_markdown),
    type: form.type,
    intents: Number(form.intents),
    shard_mode: form.type === "qq_official_v2_webhook" ? "manual" : form.shard_mode,
    shard: form.type === "qq_official_v2_webhook" || form.shard_mode === "auto" ? [0, 1] : shard,
    enable: Boolean(form.enable),
    onebot: {
      enable: Boolean(form.onebot?.enable),
      host: String(form.onebot?.host || "127.0.0.1").trim(),
      port: Number(form.onebot?.port || 5700),
      writes: Boolean(form.onebot?.writes),
    },
  };
}

export function createApi(state) {
  const bridge = () => state.bridge;
  const configPayload = (extra = {}) => {
    if (!state.current) throw new Error("当前目标还没有已加载的本地配置。");
    return {
      platform_id: state.current.platform_id,
      scene: state.scene,
      revision: state.current.revision,
      fingerprint: state.current.fingerprint,
      csrf: state.boot.csrf,
      ...extra,
    };
  };
  const connectionPayload = (extra = {}) => {
    if (!state.view || state.platformId.trim() !== state.view.platform_id) throw new Error("请先读取当前连接目标。");
    return { platform_id: state.view.platform_id, fingerprint: state.view.fingerprint, csrf: state.boot.csrf, ...extra };
  };
  return {
    configPayload,
    connectionPayload,
    async loadCatalog() {
      state.catalog = await bridge().apiGet("commands", { platform_id: state.current.platform_id, scene: state.scene });
      state.loadedScene = state.scene;
      return state.catalog;
    },
    async saveConnection(body) {
      return bridge().apiPost("connection/save", connectionPayload(body));
    },
    async reloadConnection() {
      return bridge().apiPost("connection/reload", connectionPayload({ confirm: true }));
    },
    async startScan() {
      if (["creating", "pending", "ready_to_commit"].includes(state.scan?.state)
          && state.scan.platform_id === state.view?.platform_id) {
        await bridge().apiPost("onboarding/cancel", connectionPayload({ ticket: state.scan.ticket }));
      }
      state.scan = await bridge().apiPost("onboarding/start", connectionPayload({ confirm: true }));
      return state.scan;
    },
    async refreshScan() {
      state.scan = await bridge().apiPost("onboarding/status", connectionPayload({ ticket: state.scan.ticket, renew: true }));
      return state.scan;
    },
    async cancelScan() {
      if (state.scan) state.scan = await bridge().apiPost("onboarding/cancel", connectionPayload({ ticket: state.scan.ticket }));
      return state.scan;
    },
    async commitScan() {
      const saved = await bridge().apiPost("onboarding/commit", connectionPayload({
        ticket: state.scan.ticket,
        commit_handle: state.scan.commit_handle,
        confirm: true,
        confirm_secret: true,
        confirm_identity: true,
      }));
      state.scan = saved;
      return saved;
    },
    async mutate(operation, patch, restoreRevision) {
      return bridge().apiPost("config/mutate", configPayload({
        operation,
        patch,
        confirm: operation === "apply",
        confirm_bindings: state.confirmBindings,
        restore_revision: restoreRevision,
      }));
    },
    async preview(body) {
      return bridge().apiPost("preview", configPayload(body));
    },
    async planPanels(options) {
      return bridge().apiPost("panels/plan", configPayload(options));
    },
    async enablePanels(options) {
      return bridge().apiPost("panels/enable", configPayload({ ...options, confirm: true }));
    },
    async syncPanels() {
      return bridge().apiPost("panels/sync", configPayload({ confirm: true }));
    },
    async disablePanels() {
      return bridge().apiPost("panels/disable", configPayload({ confirm: true }));
    },
    async setFlag(name, enabled) {
      const value = await bridge().apiPost("flags", {
        csrf: state.boot.csrf,
        revision: state.boot.flags_revision,
        patch: { [name]: enabled },
        confirm: true,
      });
      state.boot.flags = value.flags;
      state.boot.flags_revision = value.revision;
      return value;
    },
    async retained() {
      return bridge().apiGet("inbox/retained", { platform_id: state.current.platform_id });
    },
    async discardRetained(body) {
      return bridge().apiPost("inbox/discard", body);
    },
  };
}
