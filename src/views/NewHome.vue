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
                <div class="color" v-for="(c, i) in colors" :key="i" :style="{'background-color': c ?? '#333'}"
                     @click="e => openPicker(e, c)">
                    <div>{{(i + 1) % 10}}</div>
                </div>
            </div>

            <div class="f-grow1"></div>

            <div class="separator"/>

            <div @click="_ => toggle('sky')">Sky</div>
        </div>
        <MyColorPicker v-if="pickerColor" :color="pickerColor" style="z-index: 3"
                       @close="pickerColor = ''" :initial-pos="initialPos"
                       @updatePalette="p => colors = p[0]"/>
    </div>
</template>

<script lang="ts">
import {Options} from 'vue-class-component';
import {camera, editor, objects, start} from "@/animation/Home";
import {config} from "@/animation/Config";
import {KeyHandler, range} from "@/scripts/utils";
import MyColorPicker from "@/components/color/ColorPicker.vue";

@Options({components: {MyColorPicker}})
export default class NewHome extends KeyHandler
{
    editMode = config.editMode

    colors = localStorage.getItem('palette') ? JSON.parse(localStorage.getItem('palette') as string)[0] :
        range(10).map(_ => '#ffa8a8')

    pickerColor = ''
    initialPos = {x: 0, y: 0}
    started = false

    created(): void
    {
        // Escape to close picker
        this.keybinds.Escape = _ => this.pickerColor = ''

        // Pick colors
        range(10).forEach(i => this.keybinds[((i + 1) % 10)+''] = _ => editor.color = this.colors[i])

        // Camera position binds
        this.keybinds.ArrowLeft = _ => camera.position.x -= 1
        this.keybinds.ArrowRight = _ => camera.position.x += 1
        this.keybinds.ArrowUp = _ => camera.position.y += 1
        this.keybinds.ArrowDown = _ => camera.position.y -= 1
        this.keybinds.J = _ => camera.rotateY(Math.PI / 60)
        this.keybinds.L = _ => camera.rotateY(-Math.PI / 60)
        this.keybinds.I = _ => camera.rotateX(Math.PI / 60)
        this.keybinds.K = _ => camera.rotateX(-Math.PI / 60)
        this.keybinds.Ctrl0 = _ =>
        {
            camera.position.set(0, 0, 200)
            camera.lookAt(0, 0, 0)
        }
    }

    mounted(): void
    {
        if (!this.started)
        {
            start('three')
            this.started = true
        }
    }

    toggle(s: string): void
    {
        if (s == 'sky')
        {
            objects.hemiLight.visible = !objects.hemiLight.visible
            objects.dirLight.visible = !objects.dirLight.visible
            objects.sky.visible = !objects.sky.visible
        }
    }

    openPicker(e: MouseEvent, c: string): void
    {
        this.pickerColor = c
        this.initialPos = {x: e.clientX - 150, y: e.clientY + 50}
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
    user-select: none

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

        .color
            width: 12px
            height: 12px

            div
                margin-top: -10px
                font-size: 8px
                color: #8f8f8f

            + .color
                margin-left: 5px

#editor-controls > * + *
    margin-left: 10px

</style>
