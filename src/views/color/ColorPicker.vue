<template>
    <div id="ColorPicker" ref="window" @keydown.esc="close">
        <div id="title-colors" class="fbox-h">
            <div class="text" @mousedown="windowDrag" @click.right.alt="close">Colors</div>
            <div class="close fbox-vcenter" @click="close" v-if="showClose"><i class="fas fa-times"></i></div>
            <input v-model="colorInput" spellcheck="false" @change="colorModel = colorInput">
        </div>
        <ColorPicker id="picker" :isWidget="true" pickerType="chrome" v-model:pureColor="colorModel"
                     :disableHistory="true" @pureColorChange="change" format="hex8"/>
        <div id="palette">
            <div class="row" v-for="(p, i) of palette" :key="i">
                <div class="color" v-for="(c, j) in p" :key="j" :style="{'background-color': c ? c : '#333'}"
                     @click.exact="setPalette(i, j)"
                     @click.right.alt="(e) => altClickPalette(e, i, j)"
                     @click.right.exact="(e) => rightClickPalette(e, i, j)"
                     draggable="true" @dragstart="paletteDragStart(i, j)" @drop="(e) => dropPalette(e, i, j)"
                     @dragenter="(e) => paletteDragEnter(e, i, j)" @dragover="(e) => paletteDragOver(e, i, j)"/>
            </div>
        </div>
    </div>
</template>

<script lang="ts">
import "vue3-colorpicker/style.css";
import {ColorPicker} from "vue3-colorpicker";
import {range} from "@/utils";
import {Emit, Model} from "vue-property-decorator";
import {Vue, Options} from "vue-class-component";

@Options({components: {ColorPicker}})
export default class MyColorPicker extends Vue
{
    @Model('color') color!: string // Color prop in #ffffffff format
    colorModel = '' // Color model in #ffffffff format
    colorInput = '' // Color input in ffffff format
    palette: string[][] = []
    showClose = false

    /**
     * Init
     */
    created(): void
    {
        this.colorModel = this.color
        this.colorInput = this.colorModel.substr(1, 6)

        const storedPalette = localStorage.getItem('palette')
        this.palette = !storedPalette ? range(6).map(_ => range(10).map(_ => '')) : JSON.parse(storedPalette);
        this.storePalette()
    }

    /**
     * Color change
     */
    @Emit('update:color')
    change(color: string): string
    {
        this.colorInput = color.substr(1, 6)
        return this.colorModel
    }

    /**
     * Window dragging
     */
    windowDrag(e: MouseEvent): void
    {
        e.preventDefault()

        const el = this.$refs.window as HTMLElement
        let lastX = e.clientX, lastY = e.clientY

        const mousemove = (e: MouseEvent) =>
        {
            const dx = lastX - e.clientX, dy = lastY - e.clientY
            lastX = e.clientX; lastY = e.clientY
            el.style.left = (el.offsetLeft - dx) + 'px'
            el.style.top = (el.offsetTop - dy) + 'px'
        }
        const mouseup = () => {document.removeEventListener('mouseup', mouseup); document.removeEventListener('mousemove', mousemove)}
        document.addEventListener('mouseup', mouseup)
        document.addEventListener('mousemove', mousemove)
    }

    @Emit('updatePalette')
    storePalette(): string[][]
    {
        localStorage.setItem('palette', JSON.stringify(this.palette))
        return this.palette
    }

    @Emit()
    close(e?: Event): void
    {
        if (e) e.preventDefault()
        console.log('Color picker close')
    }

    /**
     * Left click to override
     */
    setPalette(i: number, j: number): void
    {
        this.palette[i][j] = this.colorModel
        this.storePalette()
    }

    /**
     * Right click to select
     */
    rightClickPalette(e: Event, i: number, j: number): void
    {
        e.preventDefault()
        if (!this.palette[i][j]) return
        this.colorModel = this.palette[i][j]
        this.change(this.colorModel)
    }

    /**
     * Alt right click to remove
     */
    altClickPalette(e: Event, i: number, j: number): void
    {
        e.preventDefault()
        this.palette[i][j] = ''
        this.storePalette()
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
        this.storePalette()
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

        div.close
            font-size: 10px
            margin-right: 10px

        div.text
            flex-grow: 1
            text-align: left
            user-select: none

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

        // Transparency texture
        background-image: url(data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAwAAAAMCAIAAADZF8uwAAAAGUlEQVQYV2M4gwH+YwCGIasIUwhT25BVBADtzYNYrHvv4gAAAABJRU5ErkJggg==)
        background-repeat: repeat

        .row
            width: 100%
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
