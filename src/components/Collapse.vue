<template>
    <div class="collapse" :class="{active: isActive}">
        <h3 v-if="title !== undefined" v-html="displayTitle" class="clickable ui-accordion-header"
            :class="{'ui-accordion-header-active': isActive}" @click="toggle"></h3>
        <component :is="headerTag" v-else class="ui-accordion-header"
                   :class="{'ui-accordion-header-active': isActive}" @click="toggle">
            <slot name="header"></slot>
        </component>
        <Transition name="collapse">
            <div class="content" v-show="isActive">
                <slot></slot>
            </div>
        </Transition>
    </div>
</template>

<script setup lang="ts">
import {computed, ref, watch} from 'vue'

const props = withDefaults(defineProps<{
    title?: string
    active?: boolean
    headerTag?: string
}>(), {
    title: undefined,
    active: false,
    headerTag: 'h3'
})

const emit = defineEmits<{ toggle: [active: boolean] }>()

const displayTitle = computed((): string => decodeURIComponent(props.title ?? ''))

const isActive = ref(props.active)

// Controlled mode: parent drives active (e.g. BlogPost history back/forward).
// Self-toggle still emits so the parent can sync its own state.
watch(() => props.active, (value) => {
    isActive.value = value
})

const toggle = (): void => {
    isActive.value = !isActive.value
    emit('toggle', isActive.value)
}
</script>

<style lang="sass">
.collapse
    h3.ui-accordion-header
        margin: 0
        padding-top: 0.5em
        padding-bottom: 0.5em
        user-select: none

    h3.ui-accordion-header:not(.ui-accordion-header-active):after
        content: '...'

    h3:hover
        //background-color: rgba(255, 131, 115, 0.36)

    .content
        padding-bottom: 0.5em
        overflow: hidden

    // Slide transition approximating the old jQuery-ui accordion animation
    .collapse-enter-active, .collapse-leave-active
        transition: max-height 0.25s ease, opacity 0.25s ease
        max-height: 500px

    .collapse-enter-from, .collapse-leave-to
        max-height: 0
        opacity: 0
</style>
