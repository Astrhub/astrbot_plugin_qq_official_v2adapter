<script setup>
import { computed, onMounted, ref, watch } from "vue";
import AbSelect from "./AbSelect.vue";
import { clone, normalizeProfilePolicy, normalizeStatePolicy, normalizeStoragePolicy } from "./api.js";
import { t } from "./i18n.js";

const props = defineProps({
  api: { type: Object, required: true },
  state: { type: Object, required: true },
  busy: Boolean,
  confirm: { type: Function, required: true },
});
const emit = defineEmits(["status", "run", "reload"]);

const query = ref({
  q: "",
  message_id: "",
  scene: "",
  target: "",
  since: "",
  until: "",
});
const lastSearchMode = ref("");
const entries = ref([]);
const selected = ref(null);
const exportText = ref("");
const purgeText = ref("");
const integrity = ref(null);

const modeOptions = computed(() => [
  { value: "unlimited", label: t("storage.modeUnlimited") },
  { value: "max_age", label: t("storage.modeAge") },
  { value: "max_count", label: t("storage.modeCount") },
  { value: "max_bytes", label: t("storage.modeBytes") },
]);
const sceneOptions = computed(() => [
  { value: "", label: t("storage.sceneAll") },
  { value: "group", label: t("scene.group") },
  { value: "c2c", label: t("scene.c2c") },
  { value: "channel", label: t("scene.channel") },
  { value: "dm", label: t("scene.dm") },
]);

const status = computed(() => props.state.storageStatus || {});
const policy = computed(() => props.state.storagePolicy);
const detailText = computed(() => selected.value ? JSON.stringify(selected.value, null, 2) : "");
const sceneStats = computed(() => {
  const counts = status.value.scene_counts || {};
  return Object.keys(counts).sort().map(name => `${name}: ${counts[name]}`).join(" · ") || t("storage.none");
});
const searchModeLabel = computed(() => {
  const mode = lastSearchMode.value || status.value.search?.mode;
  if (mode === "like") return t("storage.searchModeLike");
  if (mode === "fts5") return t("storage.searchModeFts");
  return t("storage.none");
});
const searchIndexLabel = computed(() => {
  const search = status.value.search;
  if (!search) return t("storage.none");
  if (search.mode === "like") return t("storage.searchModeLike");
  const pending = search.pending || 0;
  const indexed = search.indexed || 0;
  if (pending > 0) return t("storage.searchPending", { pending, indexed });
  return t("storage.searchReady", { indexed });
});

function fillStatus(value) {
  props.state.storageStatus = value;
  if (value?.policy) {
    props.state.storagePolicy = normalizeStoragePolicy(value.policy);
    props.state.storagePolicySnapshot = JSON.stringify(props.state.storagePolicy);
  }
}

async function loadStatus() {
  if (!props.state.view?.exists) {
    props.state.storageStatus = null;
    return;
  }
  fillStatus(await props.api.historyStatus());
}


function fillProfile(value) {
  props.state.profileStatus = value;
  if (value?.policy) {
    props.state.profilePolicy = normalizeProfilePolicy(value.policy);
    props.state.profilePolicySnapshot = JSON.stringify(props.state.profilePolicy);
  }
}

async function loadProfile() {
  fillProfile(await props.api.profileStatus());
}

onMounted(() => emit("run", async () => {
  await Promise.all([loadProfile(), loadState()]);
}));

const profileBounds = computed(() => props.state.profileStatus?.bounds || {});

function profilePayload() {
  const current = props.state.profilePolicy;
  return {
    max_records: Number(current.max_records),
    max_bytes: Number(current.max_bytes),
    stale_seconds: Number(current.stale_seconds),
    cooldown_seconds: Number(current.cooldown_seconds),
  };
}

async function saveProfile() {
  if (!(await props.confirm(t("storage.confirmProfile")))) return;
  const result = await props.api.saveProfilePolicy(profilePayload());
  fillProfile(result);
  emit("status", t("storage.savedProfile"));
}

function fillState(value) {
  props.state.stateStatus = value;
  if (value?.policy) {
    props.state.statePolicy = normalizeStatePolicy(value.policy);
    props.state.statePolicySnapshot = JSON.stringify(props.state.statePolicy);
  }
}

async function loadState() {
  fillState(await props.api.stateStatus());
}

const stateBounds = computed(() => props.state.stateStatus?.bounds || {});
const stateUsage = computed(() => {
  const counts = props.state.stateStatus?.counts || {};
  return t("storage.stateUsage", {
    identities: counts.identities || 0,
    targets: counts.targets || 0,
    sources: counts.sources || 0,
    deliveries: counts.deliveries || 0,
    operations: counts.operations || 0,
  });
});

function statePayload() {
  const current = props.state.statePolicy;
  return {
    retention_seconds: Number(current.retention_seconds),
    identity_capacity: Number(current.identity_capacity),
    source_capacity: Number(current.source_capacity),
    operation_capacity: Number(current.operation_capacity),
  };
}

async function saveState() {
  if (!(await props.confirm(t("storage.confirmState")))) return;
  const result = await props.api.saveStatePolicy(statePayload());
  fillState(result);
  emit("status", t("storage.savedState"));
}
watch(() => props.state.view?.platform_id, () => {
  entries.value = [];
  lastSearchMode.value = "";
  selected.value = null;
  exportText.value = "";
  integrity.value = null;
  emit("run", loadStatus);
}, { immediate: true });

function policyPayload() {
  const current = policy.value;
  const payload = { enabled: Boolean(current.enabled), mode: current.mode };
  if (current.mode === "max_age") payload.max_age_seconds = Number(current.max_age_seconds);
  else if (current.mode === "max_count") payload.max_count = Number(current.max_count);
  else if (current.mode === "max_bytes") payload.max_bytes = Number(current.max_bytes);
  return payload;
}

async function savePolicy() {
  if (!(await props.confirm(t("storage.confirmPolicy")))) return;
  const result = await props.api.saveHistoryPolicy(policyPayload());
  fillStatus(result);
  emit("status", t("storage.savedPolicy"));
}

function queryParams() {
  const params = {};
  if (query.value.q.trim()) params.q = query.value.q.trim();
  if (query.value.message_id.trim()) params.message_id = query.value.message_id.trim();
  Object.assign(params, rangeParams());
  return params;
}

function rangeParams() {
  const params = {};
  if (query.value.scene) params.scene = query.value.scene;
  if (query.value.target.trim()) params.target = query.value.target.trim();
  const since = Number(query.value.since);
  const until = Number(query.value.until);
  if (query.value.since.trim() && Number.isFinite(since)) params.since = since;
  if (query.value.until.trim() && Number.isFinite(until)) params.until = until;
  return params;
}

const canDeleteRange = computed(() => {
  const params = rangeParams();
  return Boolean(params.scene || params.target || params.since != null || params.until != null);
});

async function runQuery() {
  const result = await props.api.historyQuery(queryParams());
  entries.value = result.entries || [];
  selected.value = entries.value[0] || null;
  lastSearchMode.value = result.search_mode || "";
  emit("status", t("storage.queried", { count: entries.value.length, mode: searchModeLabel.value }));
}

async function openEntry(entry) {
  selected.value = clone(entry);
}

async function runIntegrity() {
  integrity.value = await props.api.historyIntegrity();
  emit("status", integrity.value.ok ? t("storage.integrityOk") : t("storage.integrityFailed"));
}

async function runCheckpoint() {
  if (!(await props.confirm(t("storage.confirmCheckpoint")))) return;
  const result = await props.api.historyCheckpoint();
  if (props.state.storageStatus) props.state.storageStatus.size_bytes = result.size_bytes;
  emit("status", t("storage.checkpointed"));
}

async function runExport() {
  if (!(await props.confirm(t("storage.confirmExport")))) return;
  const result = await props.api.historyExport(queryParams());
  exportText.value = JSON.stringify(result, null, 2);
  emit("status", t("storage.exported", { count: (result.messages || []).length }));
}

async function runDelete() {
  if (!canDeleteRange.value) {
    emit("status", t("storage.deleteNeedsFilter"));
    return;
  }
  if (!(await props.confirm(t("storage.confirmDelete")))) return;
  const result = await props.api.historyDelete(rangeParams());
  fillStatus(result);
  entries.value = [];
  selected.value = null;
  emit("status", t("storage.deleted", { count: result.deleted || 0 }));
}

async function runPurge() {
  if (purgeText.value !== "DELETE_MESSAGE_HISTORY") {
    emit("status", t("storage.purgeTypeRequired"));
    return;
  }
  if (!(await props.confirm(t("storage.confirmPurge")))) return;
  const result = await props.api.historyPurge(purgeText.value);
  fillStatus(result);
  entries.value = [];
  selected.value = null;
  purgeText.value = "";
  emit("status", t("storage.purged", { count: result.deleted || 0 }));
}

function formatTime(value) {
  if (value == null || value === "") return t("storage.none");
  return new Date(Number(value) * 1000).toISOString();
}
</script>

<template>
  <section class="panel-grid">
    <article class="card">
      <h2>{{ t("storage.profileTitle") }}</h2>
      <p class="muted">{{ t("storage.profileDesc") }}</p>
      <dl class="facts">
        <div>
          <dt>{{ t("storage.profileScope") }}</dt>
          <dd>{{ t("storage.profilePlugin") }}</dd>
        </div>
        <div>
          <dt>{{ t("storage.profileUsed") }}</dt>
          <dd>{{ t("storage.profileUsedCounts", { records: state.profileStatus?.used_records || 0, bytes: state.profileStatus?.used_bytes || 0 }) }}</dd>
        </div>
      </dl>
      <div class="form-grid">
        <label>
          {{ t("storage.profileMaxRecords") }}
          <input v-model.number="state.profilePolicy.max_records" type="number" :min="profileBounds.max_records?.min" :max="profileBounds.max_records?.max">
        </label>
        <label>
          {{ t("storage.profileMaxBytes") }}
          <input v-model.number="state.profilePolicy.max_bytes" type="number" :min="profileBounds.max_bytes?.min" :max="profileBounds.max_bytes?.max">
        </label>
        <label>
          {{ t("storage.profileStale") }}
          <input v-model.number="state.profilePolicy.stale_seconds" type="number" :min="profileBounds.stale_seconds?.min" :max="profileBounds.stale_seconds?.max">
        </label>
        <label>
          {{ t("storage.profileCooldown") }}
          <input v-model.number="state.profilePolicy.cooldown_seconds" type="number" :min="profileBounds.cooldown_seconds?.min" :max="profileBounds.cooldown_seconds?.max">
        </label>
      </div>
      <div class="actions">
        <button type="button" :disabled="busy" @click="emit('run', saveProfile)">
          {{ t("storage.saveProfile") }}
        </button>
      </div>
    </article>
    <article class="card">
      <h2>{{ t("storage.stateTitle") }}</h2>
      <p class="muted">{{ t("storage.stateDesc") }}</p>
      <dl class="facts">
        <div>
          <dt>{{ t("storage.profileScope") }}</dt>
          <dd>{{ t("storage.stateScope") }}</dd>
        </div>
        <div>
          <dt>{{ t("storage.profileUsed") }}</dt>
          <dd>{{ stateUsage }}</dd>
        </div>
      </dl>
      <div class="form-grid">
        <label>
          {{ t("storage.stateRetention") }}
          <input v-model.number="state.statePolicy.retention_seconds" type="number" :min="stateBounds.retention_seconds?.min" :max="stateBounds.retention_seconds?.max">
        </label>
        <label>
          {{ t("storage.stateIdentities") }}
          <input v-model.number="state.statePolicy.identity_capacity" type="number" :min="stateBounds.identity_capacity?.min" :max="stateBounds.identity_capacity?.max">
        </label>
        <label>
          {{ t("storage.stateSources") }}
          <input v-model.number="state.statePolicy.source_capacity" type="number" :min="stateBounds.source_capacity?.min" :max="stateBounds.source_capacity?.max">
        </label>
        <label>
          {{ t("storage.stateOperations") }}
          <input v-model.number="state.statePolicy.operation_capacity" type="number" :min="stateBounds.operation_capacity?.min" :max="stateBounds.operation_capacity?.max">
        </label>
      </div>
      <div class="actions">
        <button type="button" :disabled="busy" @click="emit('run', saveState)">
          {{ t("storage.saveState") }}
        </button>
      </div>
    </article>
    <article class="card">
      <h2>{{ t("storage.policyTitle") }}</h2>
      <p class="muted">{{ t("storage.policyDesc") }}</p>
      <div class="form-grid">
        <label class="wide check-row">
          <input v-model="state.storagePolicy.enabled" type="checkbox">
          {{ t("storage.enabled") }}
        </label>
        <label>
          {{ t("storage.mode") }}
          <AbSelect v-model="state.storagePolicy.mode" :options="modeOptions" />
        </label>
        <label v-if="state.storagePolicy.mode === 'max_age'">
          {{ t("storage.maxAge") }}
          <input v-model.number="state.storagePolicy.max_age_seconds" type="number" min="1">
        </label>
        <label v-if="state.storagePolicy.mode === 'max_count'">
          {{ t("storage.maxCount") }}
          <input v-model.number="state.storagePolicy.max_count" type="number" min="1">
        </label>
        <label v-if="state.storagePolicy.mode === 'max_bytes'">
          {{ t("storage.maxBytes") }}
          <input v-model.number="state.storagePolicy.max_bytes" type="number" min="1">
        </label>
      </div>
      <div class="actions">
        <button type="button" :disabled="busy || !state.view?.exists" @click="emit('run', savePolicy)">
          {{ t("storage.savePolicy") }}
        </button>
      </div>
    </article>

    <article class="card">
      <h2>{{ t("storage.statusTitle") }}</h2>
      <dl class="facts">
        <div>
          <dt>{{ t("storage.path") }}</dt>
          <dd><code class="code-tag">{{ status.path || t("storage.none") }}</code></dd>
        </div>
        <div>
          <dt>{{ t("storage.size") }}</dt>
          <dd>{{ status.size_bytes ?? t("storage.none") }}</dd>
        </div>
        <div>
          <dt>{{ t("storage.records") }}</dt>
          <dd>{{ t("storage.recordCounts", { messages: status.message_count || 0, forwards: status.forward_count || 0 }) }}</dd>
        </div>
        <div>
          <dt>{{ t("storage.scenes") }}</dt>
          <dd>{{ sceneStats }}</dd>
        </div>
        <div>
          <dt>{{ t("storage.earliest") }}</dt>
          <dd>{{ formatTime(status.earliest) }}</dd>
        </div>
        <div>
          <dt>{{ t("storage.latest") }}</dt>
          <dd>{{ formatTime(status.latest) }}</dd>
        </div>
        <div>
          <dt>{{ t("storage.wal") }}</dt>
          <dd>{{ status.wal?.mode || t("storage.none") }}</dd>
        </div>
        <div>
          <dt>{{ t("storage.schema") }}</dt>
          <dd>{{ status.schema_version ?? t("storage.none") }}</dd>
        </div>
        <div>
          <dt>{{ t("storage.searchMode") }}</dt>
          <dd>{{ searchModeLabel }}</dd>
        </div>
        <div>
          <dt>{{ t("storage.searchIndex") }}</dt>
          <dd>{{ searchIndexLabel }}</dd>
        </div>
        <div class="wide">
          <dt>{{ t("storage.lastError") }}</dt>
          <dd>{{ status.last_error || t("storage.noError") }}</dd>
        </div>
      </dl>
    </article>

    <article class="card">
      <h2>{{ t("storage.queryTitle") }}</h2>
      <div class="form-grid">
        <label class="wide">
          {{ t("storage.search") }}
          <input v-model="query.q" :placeholder="t('storage.searchPlaceholder')">
        </label>
        <label>
          {{ t("storage.messageId") }}
          <input v-model="query.message_id">
        </label>
        <label>
          {{ t("page.scene") }}
          <AbSelect v-model="query.scene" :options="sceneOptions" />
        </label>
        <label>
          {{ t("storage.target") }}
          <input v-model="query.target">
        </label>
        <label>
          {{ t("storage.since") }}
          <input v-model="query.since" :placeholder="t('storage.unixTime')">
        </label>
        <label>
          {{ t("storage.until") }}
          <input v-model="query.until" :placeholder="t('storage.unixTime')">
        </label>
      </div>
      <div class="actions">
        <button type="button" :disabled="busy || !state.view?.exists" @click="emit('run', runQuery)">
          {{ t("storage.queryBtn") }}
        </button>
      </div>
      <p class="muted">{{ t("storage.searchMode") }}: {{ searchModeLabel }}</p>
      <ul v-if="entries.length" class="storage-results">
        <li v-for="entry in entries" :key="entry.scene + entry.target + entry.message_id">
          <button type="button" class="secondary" @click="openEntry(entry)">
            {{ entry.scene }} · {{ entry.target }} · {{ entry.message_id }}
          </button>
        </li>
      </ul>
      <p v-else class="muted">{{ t("storage.noResults") }}</p>
      <pre v-if="detailText" class="storage-json">{{ detailText }}</pre>
    </article>

    <article class="card">
      <h2>{{ t("storage.maintainTitle") }}</h2>
      <p class="muted">{{ t("storage.maintainDesc") }}</p>
      <div class="actions">
        <button type="button" class="secondary" :disabled="busy || !state.view?.exists" @click="emit('run', runIntegrity)">
          {{ t("storage.integrityBtn") }}
        </button>
        <button type="button" class="secondary" :disabled="busy || !state.view?.exists" @click="emit('run', runCheckpoint)">
          {{ t("storage.checkpointBtn") }}
        </button>
        <button type="button" class="secondary" :disabled="busy || !state.view?.exists" @click="emit('run', runExport)">
          {{ t("storage.exportBtn") }}
        </button>
        <button type="button" class="secondary" :disabled="busy || !state.view?.exists || !canDeleteRange" @click="emit('run', runDelete)">
          {{ t("storage.deleteBtn") }}
        </button>
      </div>
      <p v-if="!canDeleteRange" class="muted">{{ t("storage.deleteNeedsFilter") }}</p>
      <p v-if="integrity" class="muted">{{ integrity.ok ? t("storage.integrityOk") : integrity.result }}</p>
      <pre v-if="exportText" class="storage-json">{{ exportText }}</pre>
      <label class="wide">
        {{ t("storage.purgeLabel") }}
        <input v-model="purgeText" :placeholder="t('storage.purgePlaceholder')">
      </label>
      <div class="actions">
        <button type="button" :disabled="busy || !state.view?.exists" @click="emit('run', runPurge)">
          {{ t("storage.purgeBtn") }}
        </button>
      </div>
    </article>
  </section>
</template>
