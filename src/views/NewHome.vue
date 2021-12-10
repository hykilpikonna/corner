<template>
    <div id="NewHome">
        <canvas id="three"></canvas>

        <!-- Editor controls -->
        <div id="editor-controls" class="fbox-vcenter">
            <span>Editor</span>
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
    top: 20px
    left: 0
    z-index: 2

    height: 50px
    width: calc(100vw - 100px)
    margin-left: 50px
    border-radius: 50px

    color: #ffeedb
    background-image: linear-gradient(180deg, #000000 0%, #434343 100%)
</style>
