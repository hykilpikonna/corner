<template>
    <div id="ColorPicker" ref="window">
        <div id="title-colors" class="fbox-h">
            <div @mousedown="windowDrag">Colors</div>
            <input v-model="colorInput" spellcheck="false" @change="colorModel = colorInput">
        </div>
        <ColorPicker id="picker" :isWidget="true" pickerType="chrome" v-model:pureColor="colorModel"
                     :disableHistory="true" @pureColorChange="change" format="hex"/>
        <div id="palette">
            <div class="row" v-for="(p, i) of palette" :key="i">
                <div class="color" v-for="(c, j) in p" :key="j" :style="{'background-color': c ? c : '#333'}"
                     @click="setPalette(i, j)" @contextmenu="(e) => rightClickPalette(e, i, j)"
                     @click.alt="(e) => altClickPalette(e, i, j)"
                     draggable="true" @dragstart="paletteDragStart(i, j)" @drop="(e) => dropPalette(e, i, j)"
                     @dragenter="(e) => paletteDragEnter(e, i, j)" @dragover="(e) => paletteDragOver(e, i, j)"/>
            </div>
        </div>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import "vue3-colorpicker/style.css";
import {ColorPicker} from "vue3-colorpicker";
import {Color} from "three";
import {range} from "@/utils";
import {Model} from "vue-property-decorator";

@Options({components: {ColorPicker}})
export default class MyColorPicker extends Vue
{
    @Model() color!: Color
    colorModel = ''
    colorInput = ''
    palette: string[][] = []

    /**
     * Init
     */
    created(): void
    {
        this.colorModel = '#' + this.color.getHexString()
        this.colorInput = this.colorModel.substr(1)
        this.palette = range(3).map(_ => range(10).map(_ => ''))
    }

    /**
     * Color change
     */
    change(color: string): void
    {
        this.colorInput = color.substr(1)
        const c = new Color(this.colorModel)
        this.$emit('update:color', c)
        this.$emit('updateColor', c)
    }

    /**
     * Window dragging
     */
    windowDrag(e: MouseEvent): void
    {
        e.preventDefault()

        const el = this.$refs.window as HTMLElement
        let lastX = e.clientX, lastY = e.clientY

        document.onmouseup = () => {document.onmousemove = null; document.onmouseup = null}
        document.onmousemove = (e) =>
        {
            const dx = lastX - e.clientX, dy = lastY - e.clientY
            lastX = e.clientX; lastY = e.clientY
            el.style.left = (el.offsetLeft - dx) + 'px'
            el.style.top = (el.offsetTop - dy) + 'px'
        }
    }

    /**
     * Left click to override
     */
    setPalette(i: number, j: number): void
    {
        this.palette[i][j] = this.colorModel
    }

    /**
     * Right click to select
     */
    rightClickPalette(e: Event, i: number, j: number): void
    {
        e.preventDefault()
        this.colorModel = this.palette[i][j]
        this.change(this.colorModel)
    }

    /**
     * Alt click to remove
     */
    altClickPalette(e: Event, i: number, j: number): void
    {
        e.preventDefault()
        this.palette[i][j] = ''
    }

    dragging = {i: 0, j: 0}

    paletteDragStart(i: number, j: number): void
    {
        this.dragging = {i, j}
    }

    dropPalette(e: DragEvent, i: number, j: number): void
    {
        // We can assume that toI != fromI
        const fromI = this.dragging.i * 10 + this.dragging.j
        const toI = i * 10 + j
        const incr = toI > fromI ? 1 : -1

        const currentColor = this.palette[this.dragging.i][this.dragging.j]
        for (let index of range(fromI, toI))
        {
            const col = index % 10, row = Math.floor(index / 10)
            const lastI = index + incr
            const lastC = lastI % 10, lastR = Math.floor(lastI / 10)
            console.log(lastR, lastC, 'TO', row, col)
            this.palette[row][col] = this.palette[lastR][lastC]
        }
        this.palette[i][j] = currentColor
    }

    paletteDragEnter(e: DragEvent, i: number, j: number): void
    {
        // TODO: Drag preview
        console.log('Drag enter')
        console.log(e)
        console.log('' + i + ' ' + j)
    }

    paletteDragOver(e: DragEvent, i: number, j: number): void
    {
        // Only allow drag if it's not dragging onto itself
        if (!(i == this.dragging.i && j == this.dragging.j)) e.preventDefault()
    }
}
</script>

<style lang="sass">
#ColorPicker
    position: absolute
    border-radius: 20px
    overflow: hidden

    $cp-color: #4d4d4d
    $txt-color: #cbcbcb
    background-color: $cp-color
    box-shadow: 0 0 10px #00000026
    color: $txt-color

    #title-colors
        margin: 8px 0
        font-size: 1.2em
        padding: 0 15px

        div
            flex-grow: 1
            text-align: left

        input
            background-color: lighten($cp-color, 6)
            font-family: monospace
            color: $txt-color
            border: none
            padding: 0 10px
            width: 60px
            text-align: center
            border-radius: 8px
            transition: all 0.25s ease

        input:focus-visible
            outline: none
            background-color: lighten($cp-color, 10)

    .vc-colorpicker
        width: 300px
        background-color: transparent
        box-shadow: none
        padding-bottom: 0
        border-radius: 0

    .vc-colorpicker--container
        padding: 0 3px

    .vc-chrome-colorPicker, .vc-chrome-colorPicker-body
        background-color: transparent

    .vc-display
        display: none

    .vc-saturation, .vc-saturation__white, .vc-saturation__black
        border-radius: 5px

    .vc-saturation
        height: 200px

    .vc-chrome-colorPicker-body
        margin-left: 10px
        margin-right: 10px

    #palette
        width: 300px

        .row
            width: 100%
            background-color: red
            display: flex

            .color:first-child
                border-left: none

        .row:first-child .color
            border-top: none

        .color
            width: 30px
            height: 30px
            box-sizing: border-box
            border-left: 1px solid $cp-color
            border-top: 1px solid $cp-color
            flex-grow: 1

</style>
