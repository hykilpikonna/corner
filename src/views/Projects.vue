<template>
    <div id="Projects">
        <ColorPicker v-model:color="color"></ColorPicker>
        <button @click="log"></button>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import MyColorPicker from "@/views/color/ColorPicker.vue";
import {Color} from "three";

@Options({components: {ColorPicker: MyColorPicker}})
export default class Projects extends Vue
{
    color = new Color('#ffffff')

    log(): void
    {
        console.log(this.color)
    }
}
</script>

<style lang="sass" scoped>

</style>
