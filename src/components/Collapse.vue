<template>
    <div class="collapse">
        <h3 v-html="displayTitle" class="clickable"></h3>
        <div class="content">
            <slot></slot>
        </div>
    </div>
</template>

<script setup lang="ts">
import {computed, onMounted} from 'vue'
import {$} from '@/scripts/constants';

const props = withDefaults(defineProps<{ title: string, active?: boolean }>(), {
    active: false
})

const displayTitle = computed((): string => decodeURIComponent(props.title))

onMounted((): void => {
    $('.collapse').accordion({
        collapsible: true,
        header: 'h3',
        heightStyle: 'content',
        active: props.active
    })
})
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
</style>
