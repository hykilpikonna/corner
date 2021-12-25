<template>
    <div class="collapse">
        <h3 v-html="displayTitle" class="clickable" @click="show = !show"></h3>
        <div class="content" :class="{hide: !show}">
            <slot></slot>
        </div>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {Prop} from "vue-property-decorator";

@Options({components: {}})
export default class Collapse extends Vue
{
    @Prop() title!: string

    show = false

    get displayTitle(): string
    {
        return decodeURIComponent(this.title)
    }
}
</script>

<style lang="sass">
.collapse
    .hide
        animation: fadeout 1s
        -webkit-animation: fadeout 1s
        opacity: 0
        max-height: 0
        pointer-events: none

    /* Chrome, Safari, Opera */
    @-webkit-keyframes fadeout
        from
            display: block
            visibility: visible
            opacity: 1
        to
            transform: translateY(-100px)
            display: none
            visibility: hidden
            opacity: 0
            height: 0

    *
        transition: all 0.25s ease

    h3
        background-clip: padding-box

    h3:after
        content: '...'

    h3:hover
        background-color: rgba(255, 131, 115, 0.36)
</style>
