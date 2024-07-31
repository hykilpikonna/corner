<template>
    <div class="collapse">
        <h3 v-html="displayTitle" class="clickable"></h3>
        <div class="content">
            <slot></slot>
        </div>
    </div>
</template>

<script lang="ts">
import { Component, Vue, Prop } from 'vue-facing-decorator'
import {$} from '@/scripts/constants';

@Component
export default class Collapse extends Vue
{
    @Prop title!: string
    @Prop({default: false}) active = false

    show = false

    get displayTitle(): string
    {
        return decodeURIComponent(this.title)
    }

    mounted(): void
    {
        $('.collapse').accordion({collapsible: true, header: 'h3', heightStyle: 'content',
            active: this.active})
    }
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
</style>
