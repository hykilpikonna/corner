<template>
    <table class="meta">
        <tr v-for="(v, k) in filteredTable" :key="k">
            <td>{{k}}</td>
            <td>{{v}}</td>
        </tr>
    </table>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {Prop} from "vue-property-decorator";

@Options({components: {}})
export default class MetaTable extends Vue
{
    @Prop({required: true}) table!: {[id: string]: unknown}

    get filteredTable(): {[id: string]: unknown}
    {
        const t = {...this.table}
        for (let k in Object.keys(t))
            if (!t[k]) delete t[k]
        return t
    }
}
</script>

<style lang="sass" scoped>

</style>
