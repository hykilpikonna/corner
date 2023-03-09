<template>
    <div class="publication">
        <div class="header fbox-h">
            <i class="icon fas fa-caret-right"></i>
            <div class="fbox-v">
                <div id="title">{{d.title}}</div>
                <div id="subtitle">By {{authors}}{{date.year() ? ', ' + date.year() : ''}}</div>
            </div>
        </div>
        <div id="details">
            <MetaTable id="table" :table="tableData"/>
            <div id="abstract" v-if="d.abstractNote">
                <div class="label">Abstract</div>
                <div class="content">{{d.abstractNote}}</div>
            </div>
            <div id="attachments" v-if="item.attachments.length !== 0">
                <div class="label">Attachments</div>
                <div class="content" v-for="a of item.attachments" :key="a.data.key">
                    <a :href="a.links['enclosure'].href">{{a.data.title}}</a>
                </div>
            </div>
        </div>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {Prop} from "vue-property-decorator";
import moment from "moment";
import MetaTable from "@/components/MetaTable.vue";
import {capitalize} from "@/scripts/utils";
import linkifyUrls from "linkify-urls";
import {$} from '@/scripts/constants';
import {ZoteroData, ZoteroItem} from "@/scripts/zotero";

@Options({components: {MetaTable}})
export default class ZoteroPublicationView extends Vue
{
    @Prop({required: true}) item!: ZoteroItem

    get d(): ZoteroData { return this.item.data }
    get date(): moment.Moment { return moment(this.item.meta.parsedDate) }
    get authors(): string { return this.d.creators.map(it => it.firstName + ' ' + it.lastName).join(' & ') }

    get tableData(): {[id: string]: unknown}
    {
        const t: {[id: string]: unknown} = {...this.d}
        t.creators = this.authors
        delete t.key
        delete t.version
        delete t.title
        delete t.abstractNote
        if (t.itemType) t.itemType = capitalize(t.itemType as string)
        if (t.url) t.url = linkifyUrls(t.url as string)
        return t
    }

    mounted(): void
    {
        $('.publication').accordion({collapsible: true, header: 'div.header', active: false,
            heightStyle: "content"})
    }
}
</script>

<style lang="sass" scoped>

.publication
    padding-top: 0.5em
    padding-bottom: 0.5em

    #title
        font-weight: bold

    #subtitle
        font-size: 0.9em

    #details
        padding-left: calc(1.6em + 6px)

        > div
            margin-bottom: 1em

        #table
            margin: 1em 0

        .label
            font-weight: bold

        font-size: 0.8em

.header
    align-items: center

    .icon
        transition: all 0.25s ease
        padding: 0 0.8em

.header.ui-accordion-header-active
    .icon
        transform: rotate(90deg)
</style>
