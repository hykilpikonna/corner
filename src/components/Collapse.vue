<template>
    <div class="collapse" :class="{active: isActive}">
        <component :is="headerTag" :id="headerId" class="clickable ui-accordion-header"
                   :class="{'ui-accordion-header-active': isActive}"
                   role="button" tabindex="0"
                   :aria-expanded="isActive" :aria-controls="contentId"
                   @click="toggle" @keydown="onKeydown">
            <slot name="header">
                <!-- v-html: titles may carry intentional markup (emoji <span>) -->
                <span v-if="title !== undefined" v-html="title"></span>
            </slot>
        </component>
        <Transition name="collapse">
            <div :id="contentId" class="content" role="region" :aria-labelledby="headerId" v-show="isActive">
                <slot></slot>
            </div>
        </Transition>
    </div>
</template>

<script setup lang="ts">
import {ref, useId, watch} from 'vue'

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

const uid = useId()
const headerId = `${uid}-header`
const contentId = `${uid}-content`

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

// Restores the old jQuery UI accordion keyboard behavior: Enter/Space activate,
// Arrow Up/Down cycle focus across all accordion headers on the page.
function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault()
        toggle()
        return
    }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return

    event.preventDefault()
    const headers = Array.from(document.querySelectorAll<HTMLElement>('.collapse .ui-accordion-header[role="button"]'))
    const current = event.currentTarget as HTMLElement | null
    const index = headers.indexOf(current!)
    if (index === -1 || headers.length < 2) return
    const step = event.key === 'ArrowDown' ? 1 : headers.length - 1
    headers[(index + step) % headers.length]?.focus()
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
