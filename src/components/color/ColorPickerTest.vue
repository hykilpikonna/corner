<template>
    <div id="Projects">
        <MyColorPicker v-model:color="color"></MyColorPicker>
        <button @click="log"></button>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import MyColorPicker from "@/components/color/ColorPicker.vue";

@Options({components: {MyColorPicker}})
export default class Projects extends Vue
{
    color = '#ffffff'

    log(): void
    {
        console.log(this.color)
    }
}
</script>

<style lang="sass" scoped>

</style>
