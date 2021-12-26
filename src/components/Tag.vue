<template>
    <div class="tag fbox-vcenter"><slot></slot></div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';

@Options({components: {}})
export default class Tag extends Vue
{
}
</script>

<style lang="sass" scoped>
@import src/css/colors

$tag-height: 20px
$tag-color: $color-bg-6
$text-color: $color-text-light

$padding: $tag-height / 2
$triangle-width: $tag-height / 2 * 0.8

.tag
    background: $tag-color
    border-radius: 3px 0 0 3px
    color: $text-color
    display: inline-flex
    height: $tag-height
    padding: 0 ($padding - 4) 0 $padding
    position: relative
    margin-right: $triangle-width
    transition: color 0.2s

.tag::after
    content: ''
    border-bottom: $tag-height / 2 solid transparent
    border-top: $tag-height / 2 solid transparent
    border-left: $triangle-width solid $tag-color
    position: absolute
    right: -$triangle-width
    top: 0
</style>
