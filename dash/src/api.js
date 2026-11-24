import { ref } from "vue";

import { errorFields, t } from "./i18n.js";

const CSRF_EXPIRED_MESSAGE = "Reload the page to obtain a management confirmation token.";

function isCsrfExpired(error) {
  const { code, message } = errorFields(error);
  return code === "csrf_expired" || message === CSRF_EXPIRED_MESSAGE;
}

// Official intent families exposed in the subscription dialog; bits follow v2/sdk/catalog.py.
export const KNOWN_INTENT_BITS = [
  { name: "PUBLIC_GUILD_MESSAGES", bit: 30 },
  { name: "INTERACTION", bit: 26 },
  { name: "GROUP_AND_C2C_EVENT", bit: 25 },
  { name: "DIRECT_MESSAGE", bit: 12 },
];

export const RUNTIME_POLICY_KEYS = [
  "media_max_bytes", "stream_fallback", "stream_max_chars", "stream_timeout",
  "typing_enabled", "management_writes",
];

export const COMMAND_EXTENSION_KEYS = ["ticket_ttl", "keyboard_enabled", "keyboard_execute"];


export function defaultStoragePolicy() {
  return { enabled: true, mode: "unlimited", max_age_seconds: null, max_count: null, max_bytes: null };
}

export function normalizeStoragePolicy(policy) {
  const mode = policy && policy.mode ? policy.mode : "unlimited";
  const next = defaultStoragePolicy();
  next.enabled = !policy || policy.enabled !== false;
  next.mode = mode;
  if (mode === "max_age") next.max_age_seconds = policy.max_age_seconds ?? null;
  else if (mode === "max_count") next.max_count = policy.max_count ?? null;
  else if (mode === "max_bytes") next.max_bytes = policy.max_bytes ?? null;
  return next;
}

export function defaultProfilePolicy() {
  return { max_records: 32768, max_bytes: 67108864, stale_seconds: 300, cooldown_seconds: 600 };
}

export function normalizeProfilePolicy(policy) {
  const fallback = defaultProfilePolicy();
  if (!policy || typeof policy !== "object") return fallback;
  const next = { ...fallback };
  for (const key of Object.keys(fallback)) {
    const value = Number(policy[key]);
    if (Number.isInteger(value)) next[key] = value;
  }
  return next;
}


export function defaultStatePolicy() {
  return { retention_seconds: 604800, identity_capacity: 4096, source_capacity: 16384, operation_capacity: 32768 };
}

export function normalizeStatePolicy(policy) {
  const fallback = defaultStatePolicy();
  if (!policy || typeof policy !== "object") return fallback;
  const next = { ...fallback };
  for (const key of Object.keys(fallback)) {
    const value = Number(policy[key]);
    if (Number.isInteger(value)) next[key] = value;
  }
  return next;
}

export function runtimePolicyFields(extensions = {}) {
  return Object.fromEntries(RUNTIME_POLICY_KEYS.map(key => [key, extensions[key]]));
}

export function toUint32(value) {
  return Number(value) >>> 0;
}

export function intentsHasBit(mask, bit) {
  return (toUint32(mask) >>> bit) % 2 === 1;
}

export function withIntentBit(mask, bit, enabled) {
  const base = toUint32(mask);
  if (intentsHasBit(base, bit) === Boolean(enabled)) return base;
  const delta = 2 ** bit;
  return enabled ? base + delta : base - delta;
}

export function unknownIntentBits(mask) {
  const value = toUint32(mask);
  const known = KNOWN_INTENT_BITS.reduce((sum, { bit }) => sum + (intentsHasBit(value, bit) ? 2 ** bit : 0), 0);
  return value - known;
}

// Draft state for the intents dialog: starts from the current form mask,
// mutates only explicit bits, and yields the new mask only on apply.
export function createIntentsEditor(mask) {
  const draft = ref(toUint32(Number(mask) || 0));
  const valid = () => Number.isInteger(draft.value) && draft.value >= 0 && draft.value <= 4294967295;
  return {
    get value() { return draft.value; },
    get valid() { return valid(); },
    has(bit) { return valid() && intentsHasBit(draft.value, bit); },
    unknownBits() { return valid() ? unknownIntentBits(draft.value) : 0; },
    toggle(bit, enabled) { if (valid()) draft.value = withIntentBit(draft.value, bit, enabled); },
    setDecimal(raw) {
      const value = Number(raw);
      draft.value = raw === "" || raw === null || !Number.isInteger(value) || value < 0 || value > 4294967295 ? NaN : value;
    },
    decimalText() { return Number.isInteger(draft.value) ? String(draft.value) : ""; },
    apply() { return valid() ? draft.value : null; },
  };
}

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

export function draftPatch(state) {
  const commandExtensions = Object.fromEntries(COMMAND_EXTENSION_KEYS.map(key => [key, state.extensions[key]]));
  state.draft.extensions = { ...state.draft.extensions, ...commandExtensions };
  return changed(state.current.draft, state.draft);
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

function csrfRefreshFailed() {
  const error = new Error(t("msg.csrfRefreshFailed"));
  error.code = "csrf_refresh_failed";
  return error;
}

export function createApi(state) {
  const get = (endpoint, params = {}) => state.bridge.apiGet(endpoint, params);
  async function refreshCsrf() {
    const boot = await get("bootstrap");
    const csrf = typeof boot?.csrf === "string" ? boot.csrf.trim() : "";
    if (!csrf) throw csrfRefreshFailed();
    state.boot.csrf = csrf;
  }
  const post = async (endpoint, body = {}) => {
    try {
      return await state.bridge.apiPost(endpoint, { csrf: state.boot.csrf, ...body });
    } catch (error) {
      if (!isCsrfExpired(error)) throw error;
      try {
        await refreshCsrf();
      } catch (refreshError) {
        throw refreshError?.code === "csrf_refresh_failed" ? refreshError : csrfRefreshFailed();
      }
      const retry = new Error(t("msg.csrfExpiredRetry"));
      retry.code = "csrf_expired";
      throw retry;
    }
  };
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
    async historyStatus() {
      needView();
      return get("history/status", { platform_id: state.platformId });
    },
    async historyQuery(params = {}) {
      needView();
      return get("history/query", { platform_id: state.platformId, ...params });
    },
    async saveHistoryPolicy(policy) {
      needView();
      return post("history/policy", {
        platform_id: state.view.platform_id,
        fingerprint: state.view.fingerprint,
        confirm: true,
        policy,
      });
    },
    async historyIntegrity() {
      needView();
      return post("history/integrity", {
        platform_id: state.view.platform_id,
        fingerprint: state.view.fingerprint,
      });
    },
    async historyCheckpoint() {
      needView();
      return post("history/checkpoint", {
        platform_id: state.view.platform_id,
        fingerprint: state.view.fingerprint,
        confirm: true,
      });
    },
    async historyExport(params = {}) {
      needView();
      return post("history/export", {
        platform_id: state.view.platform_id,
        fingerprint: state.view.fingerprint,
        confirm: true,
        ...params,
      });
    },
    async historyDelete(params = {}) {
      needView();
      return post("history/delete", {
        platform_id: state.view.platform_id,
        fingerprint: state.view.fingerprint,
        confirm: true,
        confirm_delete: true,
        ...params,
      });
    },
    async historyPurge(confirmText) {
      needView();
      return post("history/purge", {
        platform_id: state.view.platform_id,
        fingerprint: state.view.fingerprint,
        confirm: true,
        confirm_text: confirmText,
      });
    },
    async profileStatus() {
      return get("profile/status");
    },
    async saveProfilePolicy(policy) {
      return post("profile/policy", { confirm: true, policy });
    },
    async stateStatus() {
      return get("state/status");
    },
    async saveStatePolicy(policy) {
      return post("state/policy", { confirm: true, policy });
    },
  };
}
