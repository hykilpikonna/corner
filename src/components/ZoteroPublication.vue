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
                    <a :href="a.links['enclosure']?.href">{{a.data.title}}</a>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import {computed, onMounted} from 'vue'
import moment from "moment";
import MetaTable from "@/components/MetaTable.vue";
import {capitalize} from "@/scripts/utils";
import {linkifyUrlsToHtml} from "linkify-urls";
import {$} from '@/scripts/constants';
import {ZoteroData, ZoteroItem} from "@/scripts/zotero";

const props = defineProps<{ item: ZoteroItem }>()

const d = computed((): ZoteroData => props.item.data)
const date = computed((): moment.Moment => moment(props.item.meta.parsedDate))
const authors = computed((): string => d.value.creators.map(it => it.firstName + ' ' + it.lastName).join(' & '))

const tableData = computed((): {[id: string]: unknown} => {
    const t: {[id: string]: unknown} = {...d.value}
    t.creators = authors.value
    delete t.key
    delete t.version
    delete t.title
    delete t.abstractNote
    if (t.itemType) t.itemType = capitalize(t.itemType as string)
    if (t.url) t.url = linkifyUrlsToHtml(t.url as string)
    return t
})

onMounted((): void => {
    $('.publication').accordion({
        collapsible: true,
        header: 'div.header',
        active: false,
        heightStyle: 'content'
    })
})
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
        font-size: 0.8em

        > div
            margin-bottom: 1em

        #table
            margin: 1em 0

        .label
            font-weight: bold

.header
    align-items: center

    .icon
        transition: all 0.25s ease
        padding: 0 0.8em

.header.ui-accordion-header-active
    .icon
        transform: rotate(90deg)
</style>
