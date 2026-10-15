<script setup>
import { computed, ref } from "vue";
import AbSelect from "./AbSelect.vue";
import { t, tState } from "./i18n.js";

const props = defineProps({
  api: { type: Object, required: true },
  state: { type: Object, required: true },
  busy: Boolean,
  confirm: { type: Function, required: true },
});
const emit = defineEmits(["status", "run"]);
const output = ref(null);
const plan = ref(null);
const retained = ref(null);
const picked = ref([]);

const remoteState = computed(() => props.state.current?.remote_state?.[props.state.scene] || { state: "not_managed" });
const targetTypeOptions = computed(() => [
  { value: "all", label: t("remote.targetAll") },
  { value: "specific", label: t("remote.targetSpecific") },
]);

const activeInfo = computed(() => {
  const src = output.value || remoteState.value || {};
  const st = src.state?.state || src.state || remoteState.value.state || "not_managed";
  const panelId = src.panel_id || src.state?.panel_id || remoteState.value.panel_id || "";
  const pending = src.pending || src.state?.pending || remoteState.value.pending || null;
  const error = (typeof src.error === "object" && src.error) || (typeof remoteState.value.error === "object" && remoteState.value.error) || null;
  const issues = src.issues || [];
  const items = src.payload?.panel?.items || [];
  const fingerprint = src.fingerprint || src.plan_fingerprint || remoteState.value.plan_fingerprint || "";
  const revision = src.applied_revision ?? remoteState.value.applied_revision ?? "—";
  const catalogVer = src.catalog_version || remoteState.value.catalog_version || "—";
  return { st, panelId, pending, error, issues, items, fingerprint, revision, catalogVer };
});

function options() {
  return {
    target_type: props.state.panelTargetType,
    targets: props.state.panelTargets.split(",").map(item => item.trim()).filter(Boolean),
    menu_only: props.state.menuOnly,
  };
}
async function toggleGate() {
  const enabled = !props.state.boot.flags?.remote_menu_sync;
  if (!(await props.confirm(enabled ? t("remote.confirmGateOpen") : t("remote.confirmGateClose")))) return;
  await props.api.setFlag("remote_menu_sync", enabled);
  emit("status", enabled ? t("remote.gateOpened") : t("remote.gateClosed"));
}
async function makePlan() {
  plan.value = await props.api.planPanels(options());
  output.value = plan.value;
  emit("status", t("remote.plannedStatus"));
}
async function enable() {
  if (!props.state.boot.flags?.remote_menu_sync) throw new Error(t("remote.errOpenGateFirst"));
  if (!plan.value) throw new Error(t("remote.errPlanFirst"));
  if (plan.value.issues?.length) throw new Error(t("remote.errPlanIssues"));
  if (!(await props.confirm(t("remote.confirmEnable", { scene: t(`scene.${props.state.scene}`) })))) return;
  output.value = await props.api.enablePanels({ ...options(), plan_fingerprint: plan.value.fingerprint });
  plan.value = null;
  emit("status", t("remote.enabledStatus"));
}
async function sync() {
  if (!(await props.confirm(t("remote.confirmSync")))) return;
  output.value = await props.api.syncPanels();
  emit("status", t("remote.syncedStatus"));
}
async function disable() {
  if (!(await props.confirm(t("remote.confirmDisable")))) return;
  output.value = await props.api.disablePanels();
  emit("status", t("remote.disabledStatus"));
}
async function loadRetained() {
  retained.value = await props.api.retained();
  picked.value = [];
}
async function discard() {
  if (!retained.value) throw new Error(t("remote.errLoadRetainedFirst"));
  const entries = retained.value.entries.filter(entry => picked.value.includes(entry.receipt)).map(({ receipt, version, confirmation }) => ({ receipt, version, confirmation }));
  if (!entries.length) throw new Error(t("remote.errSelectRetained"));
  if (!(await props.confirm(t("remote.confirmDiscardRetained", { count: entries.length })))) return;
  const result = await props.api.discardRetained({
    platform_id: retained.value.platform_id,
    fingerprint: retained.value.fingerprint,
    expires: retained.value.expires,
    csrf: props.state.boot.csrf,
    entries,
    confirm: true,
  });
  await loadRetained();
  emit("status", t("remote.discardedStatus", { count: result.discarded }));
}
</script>

<template>
  <section class="panel-grid remote-grid">
    <article class="card">
      <div class="card-head">
        <h2>{{ t("remote.panelTitle") }}</h2>
        <button type="button" class="secondary" :disabled="busy" @click="emit('run', toggleGate)">
          {{ state.boot?.flags?.remote_menu_sync ? t("remote.gateClose") : t("remote.gateOpen") }}
        </button>
      </div>

      <div class="form-grid">
        <label>{{ t("remote.targetType") }}<AbSelect v-model="state.panelTargetType" :options="targetTypeOptions" /></label>
        <label>{{ t("remote.targetOpenids") }}<input v-model="state.panelTargets" maxlength="10400" :placeholder="t('remote.targetPlaceholder')"></label>
      </div>
      <label class="check-row"><input v-model="state.menuOnly" type="checkbox"> {{ t("remote.menuOnly") }}</label>

      <div class="actions" style="margin-top: 12px;">
        <button type="button" :disabled="busy" @click="emit('run', makePlan)">{{ t("remote.planBtn") }}</button>
        <button type="button" :disabled="busy" @click="emit('run', enable)">{{ t("remote.enableBtn") }}</button>
        <button type="button" class="secondary" :disabled="busy" @click="emit('run', sync)">{{ t("remote.syncBtn") }}</button>
        <button type="button" class="secondary" :disabled="busy" @click="emit('run', disable)">{{ t("remote.disableBtn") }}</button>
      </div>

      <div class="subcard">
        <h3>{{ t("remote.inspectTitle") }}</h3>
        <dl class="facts">
          <div>
            <dt>{{ t("remote.currentState") }}</dt>
            <dd>
              <span class="badge" :class="activeInfo.st === 'synced' ? 'badge-ok' : activeInfo.st === 'not_managed' ? 'badge-muted' : 'badge-warn'">
                {{ tState(activeInfo.st) }}
              </span>
            </dd>
          </div>
          <div>
            <dt>Panel ID</dt>
            <dd><code>{{ activeInfo.panelId || "—" }}</code></dd>
          </div>
          <div>
            <dt>{{ t("remote.fieldRevision") }}</dt>
            <dd>{{ activeInfo.revision }}</dd>
          </div>
          <div>
            <dt>{{ t("remote.fieldIssues") }}</dt>
            <dd>
              <div v-if="activeInfo.issues.length" class="chip-row">
                <code v-for="iss in activeInfo.issues" :key="iss" class="code-tag danger-tag">{{ iss }}</code>
              </div>
              <span v-else class="badge badge-ok">{{ t("remote.issuesOk") }}</span>
            </dd>
          </div>
        </dl>

        <div v-if="activeInfo.pending" class="state-banner">
          <span>{{ t("remote.pendingKind") }}：<strong>{{ tState(activeInfo.pending.kind) }}</strong></span>
          <code v-if="activeInfo.pending.op_id" class="code-tag">{{ activeInfo.pending.op_id.slice(0, 12) }}</code>
        </div>
        <div v-if="activeInfo.error" class="state-banner">
          <span class="code-tag danger-tag">{{ activeInfo.error.code }}</span>
          <code v-if="activeInfo.error.business_code" class="code-tag">err={{ activeInfo.error.business_code }}</code>
          <code v-if="activeInfo.error.trace_id" class="code-tag">trace_id={{ activeInfo.error.trace_id }}</code>
        </div>

        <h4>{{ t("remote.itemsTitle") }} ({{ activeInfo.items.length }})</h4>
        <div v-if="activeInfo.items.length" class="panel-items-grid">
          <div v-for="item in activeInfo.items" :key="item.name" class="panel-item-card">
            <div class="panel-item-top">
              <code>{{ item.name }}</code>
              <span v-if="item.only_admin" class="badge badge-warn">{{ t("remote.adminOnly") }}</span>
            </div>
            <span class="muted">{{ item.desc }}</span>
          </div>
        </div>
        <p v-else class="muted">{{ t("remote.noItems") }}</p>
      </div>
    </article>

    <article class="card">
      <div class="card-head">
        <h2>{{ t("remote.retainedTitle") }}</h2>
        <button type="button" class="secondary" :disabled="busy" @click="emit('run', loadRetained)">{{ t("remote.loadRetainedBtn") }}</button>
      </div>
      <div v-if="retained?.entries?.length" class="retained-list">
        <label v-for="entry in retained.entries" :key="entry.receipt" class="retained-row">
          <input v-model="picked" :value="entry.receipt" type="checkbox">
          <code>#{{ entry.receipt }}</code>
          <span class="badge badge-primary">{{ entry.event_type }}</span>
          <span class="badge badge-warn">{{ tState(entry.state) }}</span>
          <span class="muted">{{ entry.reason }}</span>
        </label>
      </div>
      <p v-else class="muted" style="margin: 14px 0;">{{ t("remote.noRetained") }}</p>
      <button type="button" class="danger" :disabled="busy || !retained?.entries?.length" @click="emit('run', discard)">{{ t("remote.discardBtn") }}</button>

      <div class="subcard">
        <h3>{{ t("remote.extOpsTitle") }}</h3>
        <div v-if="state.current?.extension_state?.operations?.length" class="retained-list">
          <div v-for="op in state.current.extension_state.operations" :key="op.op_id" class="retained-row">
            <code>{{ op.op_id.slice(0, 12) }}</code>
            <span class="badge badge-primary">{{ op.kind }}</span>
            <span class="badge" :class="op.state === 'succeeded' ? 'badge-ok' : op.state === 'unknown' || op.state === 'partial' ? 'badge-warn' : 'badge-muted'">
              {{ tState(op.state) }}
            </span>
            <code v-if="op.context?.path" class="code-tag">{{ op.context.method }} {{ op.context.path }}</code>
          </div>
        </div>
        <p v-else class="muted">{{ t("remote.noExtOps") }}</p>
      </div>
    </article>
  </section>
</template>
