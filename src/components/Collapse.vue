<template>
    <div class="collapse">
        <h3 v-html="displayTitle"></h3>
        <div class="content">
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

    get displayTitle(): string
    {
        return decodeURIComponent(this.title)
    }
}
</script>

<style lang="sass">
.collapse
    .content
        display: none
</style>
