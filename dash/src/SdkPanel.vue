<script setup>
import { computed, ref } from "vue";
import { t, tState } from "./i18n.js";

const props = defineProps({ state: { type: Object, required: true } });
const emit = defineEmits(["status"]);
const query = ref("");
const category = ref("all");

const cap = computed(() => props.state.view?.capabilities || {});
const sdk = computed(() => cap.value.sdk || {});
const sending = computed(() => cap.value.sending || {});
const mediaMatrix = computed(() => (typeof sending.value.media === "object" && sending.value.media) || {});
const methods = computed(() => sdk.value.native_implemented || []);

function classify(name) {
  if (/upload|_file$/.test(name)) return "media";
  if (/panel|menu|gateway|ws_url|reaction|interaction|^me$/.test(name)) return "panel";
  if (/group|join_approval/.test(name) && !/message|file|upload/.test(name)) return "group";
  if (/guild|channel|mute|role|announce|pin|schedule|thread|audio|mic|dms/.test(name) && !/post_message|post_dms|post_keyboard|recall|patch_guild_message|get_message/.test(name)) return "guild";
  return "message";
}

const filteredMethods = computed(() => {
  const q = query.value.trim().toLowerCase();
  return methods.value.filter(name => {
    if (category.value !== "all" && classify(name) !== category.value) return false;
    return !q || name.toLowerCase().includes(q);
  });
});

const actionsList = computed(() => {
  const raw = cap.value.actions || {};
  return Object.entries(raw).map(([name, info]) => ({
    name,
    support: info.support || "unknown",
    params: Object.keys(info.parameters || {}),
    returns: info.returns || [],
  }));
});

function copyMethod(name) {
  navigator.clipboard?.writeText?.(`event.bot.qq.${name}`)?.catch?.(() => {});
  emit("status", t("sdk.copied", { name: `event.bot.qq.${name}` }));
}
</script>

<template>
  <section class="sdk-page">
    <div class="facts facts-4">
      <div>
        <dt>{{ t("sdk.entry") }}</dt>
        <dd><code>event.bot.qq</code> / <code>event.bot.api</code></dd>
      </div>
      <div>
        <dt>{{ t("sdk.coverage") }}</dt>
        <dd>{{ t("sdk.coverageVal", { http: sdk.http_targets || 96, events: sdk.business_events || 56 }) }}</dd>
      </div>
      <div>
        <dt>{{ t("sdk.methodsCount") }}</dt>
        <dd>{{ t("sdk.methodsVal", { native: methods.length || 88, legacy: sdk.legacy_sdk_methods || 64 }) }}</dd>
      </div>
      <div>
        <dt>{{ t("sdk.subsystems") }}</dt>
        <dd class="chip-row">
          <span class="badge" :class="cap.transport === 'implemented' ? 'badge-ok' : 'badge-muted'">HTTP/WS</span>
          <span class="badge" :class="sdk.profiles === 'durable' ? 'badge-ok' : 'badge-muted'">Profiles</span>
          <span class="badge" :class="sdk.event_observation === 'live_bounded' ? 'badge-ok' : 'badge-muted'">EventBus</span>
        </dd>
      </div>
    </div>

    <div class="panel-grid sdk-grid">
      <article class="card">
        <div class="card-head">
          <h2>{{ t("sdk.nativeTitle") }}</h2>
          <input v-model="query" class="sdk-search" :placeholder="t('sdk.searchPlaceholder')">
        </div>
        <div class="filter-pills">
          <button
            v-for="cat in ['all', 'message', 'group', 'guild', 'media', 'panel']"
            :key="cat"
            type="button"
            class="pill-btn"
            :class="{ active: category === cat }"
            @click="category = cat"
          >
            {{ t(`sdk.cat.${cat}`) }}
          </button>
        </div>
        <div class="method-grid">
          <button
            v-for="name in filteredMethods"
            :key="name"
            type="button"
            class="method-chip"
            @click="copyMethod(name)"
          >
            <code>{{ name }}</code>
            <span class="badge badge-muted">{{ t(`sdk.cat.${classify(name)}`) }}</span>
          </button>
        </div>
      </article>

      <div class="sdk-side">
        <article class="card">
          <h2>{{ t("sdk.policyTitle") }}</h2>
          <div class="kv-list">
            <div class="kv-row">
              <span>{{ t("sdk.scenes") }}</span>
              <div class="chip-row">
                <span v-for="sc in ['group', 'c2c', 'channel', 'dm']" :key="sc" class="badge badge-primary">{{ t(`scene.${sc}`) }}</span>
              </div>
            </div>
            <div class="kv-row">
              <span>{{ t("sdk.activeFallback") }}</span>
              <div class="chip-row">
                <code v-for="code in sending.active_fallback?.rejected_codes || [304103, 40034005, 40034026, 40034128]" :key="code" class="code-tag">{{ code }}</code>
              </div>
            </div>
            <div class="kv-row">
              <span>{{ t("sdk.sessionFallback") }}</span>
              <code class="code-tag">{{ sending.session_fallback?.trigger_business_code || 40034105 }}</code>
            </div>
            <div class="kv-row">
              <span>{{ t("sdk.streaming") }}</span>
              <span class="muted">{{ t("sdk.streamingVal") }}</span>
            </div>
            <div class="kv-row">
              <span>{{ t("sdk.mediaGroup") }}</span>
              <div class="chip-row">
                <code v-for="m in mediaMatrix.group_c2c || ['image', 'record', 'video', 'file']" :key="m" class="code-tag">{{ m }}</code>
              </div>
            </div>
            <div class="kv-row">
              <span>{{ t("sdk.mediaChannel") }}</span>
              <div class="chip-row">
                <code v-for="m in mediaMatrix.channel || ['image_http_url', 'image_multipart']" :key="m" class="code-tag">{{ m }}</code>
              </div>
            </div>
            <div class="kv-row">
              <span>{{ t("sdk.mediaDm") }}</span>
              <div class="chip-row">
                <code v-for="m in mediaMatrix.dm || ['image_http_url']" :key="m" class="code-tag">{{ m }}</code>
              </div>
            </div>
          </div>
        </article>

        <article class="card">
          <h2>{{ t("sdk.actionsTitle") }}</h2>
          <div class="action-table-wrap">
            <table class="ab-table">
              <thead>
                <tr>
                  <th>{{ t("sdk.colAction") }}</th>
                  <th>{{ t("sdk.colSupport") }}</th>
                  <th>{{ t("sdk.colReturns") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="act in actionsList" :key="act.name">
                  <td><code>{{ act.name }}</code></td>
                  <td>
                    <span class="badge" :class="act.support === 'supported' ? 'badge-ok' : act.support === 'conditional' || act.support === 'partial' ? 'badge-warn' : 'badge-muted'">
                      {{ tState(act.support) }}
                    </span>
                  </td>
                  <td>
                    <div class="chip-row">
                      <code v-for="ret in act.returns" :key="ret" class="code-tag">{{ ret }}</code>
                      <span v-if="!act.returns.length" class="muted">—</span>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </article>
      </div>
    </div>
  </section>
</template>
