<template>
    <div id="NewHome">
        <canvas id="three"></canvas>

        <!-- Editor controls -->
        <div id="editor-controls">
            <div>Editor</div>

            <div class="separator"/>

            <div>New</div>
            <div>Save</div>

            <div class="separator"/>

            <div id="colors" class="fbox-h">
                <span class="colors-text">Colors</span>
                <div class="color" v-for="(c, i) in colors" :key="i" :style="{'background-color': c}">
                    <div>{{i + 1}}</div>
                </div>
            </div>
        </div>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {start} from "@/animation/Home";
import {config} from "@/animation/Config";

@Options({components: {}})
export default class NewHome extends Vue
{
    editMode = config.editMode

    colors = [
        '#ffa8a8',
        '#ffa8a8',
        '#ffa8a8',
        '#ffa8a8',
        '#ffa8a8',
        '#ffa8a8',
        '#ffa8a8',
        '#ffa8a8',
        '#ffa8a8',
    ]

    mounted(): void
    {
        start('three')
    }
}
</script>

<style lang="sass" scoped>
#three
    width: 100vw
    height: 100vh
    position: absolute
    top: 0
    left: 0
    z-index: 1
    cursor: none

#editor-controls
    position: absolute
    z-index: 2

    // Positioning
    top: 20px
    left: 50px
    height: 50px
    width: calc(100vw - 100px - 40px)
    padding: 0 20px
    border-radius: 50px

    // Flex center
    display: flex
    align-items: center

    // Colors
    color: #ffeedb
    background-image: linear-gradient(180deg, #000000 0%, #434343 100%)

    // Separator
    .separator
        width: 1px
        height: 50%
        border-radius: 100px
        background-color: rgba(255, 238, 219, 0.37)

    #colors
        align-items: center
        .colors-text
            margin-right: 5px

        .color
            width: 12px
            height: 12px
            margin-left: 5px

            div
                margin-top: -10px
                font-size: 8px
                color: #8f8f8f

#editor-controls > *
    margin-right: 10px

</style>
