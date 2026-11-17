<script setup>
import { computed } from "vue";
import AbSelect from "./AbSelect.vue";
import { draftPatch } from "./api.js";
import { t } from "./i18n.js";

const props = defineProps({
  api: { type: Object, required: true },
  state: { type: Object, required: true },
  busy: Boolean,
  confirm: { type: Function, required: true },
});
const emit = defineEmits(["run", "reload"]);

const versionOptions = computed(() => (props.state.current?.versions || []).map(version => ({
  value: version,
  label: t("cmd.versionItem", { v: version }),
})));

async function mutate(operation) {
  const patch = draftPatch(props.state);
  if (operation === "apply" && Object.keys(patch).length) throw new Error(t("cmd.errSaveBeforeApply"));
  if (operation !== "save" && !(await props.confirm(t("cmd.confirmMutate")))) return;
  await props.api.mutate(operation, patch, Number(props.state.restoreRevision));
  emit("reload");
}

function run(operation) {
  emit("run", () => mutate(operation));
}
</script>

<template>
  <div class="draft-actions">
    <div class="actions" style="margin-top: 14px;">
      <button type="button" :disabled="busy" @click="run('save')">{{ t("cmd.saveDraft") }}</button>
      <button type="button" class="secondary" :disabled="busy" @click="run('apply')">{{ t("cmd.applyLocal") }}</button>
      <button type="button" class="secondary" :disabled="busy" @click="run('discard')">{{ t("cmd.discardDraft") }}</button>
      <button type="button" class="secondary" :disabled="busy" @click="run('defaults')">{{ t("cmd.restoreDefaults") }}</button>
    </div>
    <div v-if="versionOptions.length" class="form-grid" style="align-items: end; margin-top: 12px;">
      <label>{{ t("cmd.retainedVersions") }}<AbSelect v-model="state.restoreRevision" :options="versionOptions" /></label>
      <button type="button" class="secondary" :disabled="busy" @click="run('restore')">{{ t("cmd.restoreBtn") }}</button>
    </div>
  </div>
</template>
