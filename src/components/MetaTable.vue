<template>
    <table class="meta">
        <tr v-for="(v, k) in filteredTable" :key="k">
            <td>{{k}}</td>
            <td v-html="v"></td>
        </tr>
    </table>
</template>

<script lang="ts">
import { Component, Vue, Prop, toNative } from 'vue-facing-decorator'

@Component
class MetaTable extends Vue
{
    @Prop({required: true}) table!: {[id: string]: unknown}

    get filteredTable(): {[id: string]: unknown}
    {
        const t: {[id: string]: unknown} = {}

        Object.keys(this.table).forEach(k => {
            // Ignore empty
            if (!this.table[k]) return

            // Convert to sentence case (https://stackoverflow.com/a/7225450/7346633)
            let newK = k.replace(/([A-Z])/g, " $1")
            newK = newK.charAt(0).toUpperCase() + newK.slice(1)
            t[newK] = this.table[k]
        })

        return t
    }
}

export default toNative(MetaTable)
</script>

<style lang="sass" scoped>
@use "../css/colors"

.meta
    td:first-child
        text-align: right
        color: colors.$color-text-light

    td:last-child
        display: inline-block
        margin-left: 20px
</style>
