<template>
    <div class="tag-wrap">
        <div class="tag fbox-vcenter" :class="direction">
            <slot></slot>
            <div class="after"></div>
        </div>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {Prop} from "vue-property-decorator";

@Options({components: {}})
export default class Tag extends Vue
{
    @Prop({default: 'left'}) direction: 'left' | 'right' = 'left'
}
</script>

<style lang="sass" scoped>
@import src/css/colors

$tag-height: 20px
$tag-color: $color-bg-6
$text-color: $color-text-light

$padding: $tag-height / 2
$triangle-width: $tag-height / 2 * 0.8

.tag-wrap
    display: inline-block

.tag
    background: $tag-color
    color: $text-color
    display: inline-flex
    height: $tag-height
    position: relative
    transition: color 0.2s

    .after
        border-bottom: $tag-height / 2 solid transparent
        border-top: $tag-height / 2 solid transparent
        border-left: $triangle-width solid $tag-color
        position: absolute
        top: 0

.tag.right
    padding: 0 ($padding - 4) 0 $padding
    margin-right: $triangle-width
    border-radius: 3px 0 0 3px

    .after
        right: -$triangle-width

.tag.left
    padding: 0 $padding 0 ($padding - 4)
    margin-left: $triangle-width
    border-radius: 0 3px 3px 0

    .after
        transform: rotate(180deg)
        left: -$triangle-width
</style>
