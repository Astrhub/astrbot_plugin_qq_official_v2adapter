<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

const props = defineProps({
  modelValue: { type: [String, Number], default: "" },
  options: { type: Array, default: () => [] },
  editable: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
  placeholder: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue", "change", "enter"]);
const open = ref(false);
const root = ref(null);

const currentLabel = computed(() => {
  const hit = props.options.find(item => String(item.value) === String(props.modelValue));
  return hit ? hit.label : String(props.modelValue ?? "");
});

function toggle() {
  if (props.disabled) return;
  open.value = !open.value;
}

function choose(item) {
  open.value = false;
  if ( item.value !== props.modelValue) {
    emit("update:modelValue", item.value);
    emit("change", item.value);
  }
}

function onInput(event) {
  emit("update:modelValue", event.target.value);
}

function onOutside(event) {
  if (root.value && !root.value.contains(event.target)) {
    open.value = false;
  }
}

onMounted(() => document.addEventListener("pointerdown", onOutside));
onBeforeUnmount(() => document.removeEventListener("pointerdown", onOutside));
</script>

<template>
  <div ref="root" class="ab-select" :class="{ open, disabled }">
    <div v-if="editable" class="ab-select-combo">
      <input
        :value="modelValue"
        :disabled="disabled"
        :placeholder="placeholder"
        maxlength="128"
        autocomplete="off"
        @input="onInput"
        @focus="open = options.length > 0"
        @keyup.enter="open = false; emit('enter')"
      >
      <button v-if="options.length" type="button" class="ab-select-arrow-btn" :disabled="disabled" tabindex="-1" @click="toggle">
        <svg class="ab-select-chevron" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <path d="M5.5 7.5 10 12l4.5-4.5" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </button>
    </div>
    <button v-else type="button" class="ab-select-trigger" :disabled="disabled" @click="toggle">
      <span class="ab-select-text">{{ currentLabel || placeholder }}</span>
      <svg class="ab-select-chevron" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
        <path d="M5.5 7.5 10 12l4.5-4.5" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    </button>

    <ul v-if="open && options.length" class="ab-select-menu" role="listbox">
      <li
        v-for="item in options"
        :key="item.value"
        class="ab-select-option"
        :class="{ selected: String(item.value) === String(modelValue) }"
        role="option"
        @click="choose(item)"
      >
        <div class="ab-select-option-main">
          <span>{{ item.label }}</span>
          <small v-if="item.sublabel" class="muted">{{ item.sublabel }}</small>
        </div>
        <svg v-if="String(item.value) === String(modelValue)" class="ab-select-check" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="m4.5 10.5 3.5 3.5 7.5-7.5" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </li>
    </ul>
  </div>
</template>
