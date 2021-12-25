<template>
    <div id="Publication">
        <div id="title">{{d.title}}</div>
        <div id="subtitle">By {{d.creators.map(it => it.firstName + ' ' + it.lastName).join(' & ')}}{{date ? ', ' + date.year() : ''}}</div>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';
import {Prop} from "vue-property-decorator";
import moment from "moment";

export interface ZoteroLink
{
    href: string // 'https://...'
    type: string // 'text/html'
    attachmentType?: string // 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    attachmentSize?: number // bytes?
}

export type ZoteroLinks = {[id: string]: ZoteroLink}

export interface ZoteroCreator
{
    creatorType: string // 'author'
    firstName: string
    lastName: string
}

export interface ZoteroData
{
    key: string
    itemType: string // 'document'
    title: string
    creators: ZoteroCreator[]
    abstractNote?: string
    publisher?: string
    date?: string // 'Dec 9 2021'
    language?: string // 'en'
    shortTitle?: string
    url?: string
    accessDate?: string
    archive?: string
    archiveLocation?: string
    libraryCatalog?: string
    rights?: string
    extra?: string
}

export interface ZoteroItem
{
    key: string
    version: number
    library: {
        type: string // 'user'
        id: number
        name: string
        links: ZoteroLinks
    }
    meta: {
        creatorSummary: string // Gui and Burton
        parsedDate: string // 2021-12-09
        numChildren: 1
    }
    data: ZoteroData
}

@Options({components: {}})
export default class ZoteroPublicationView extends Vue
{
    @Prop({required: true}) item!: ZoteroItem

    get d(): ZoteroData { return this.item.data }

    get date(): moment.Moment { return moment(this.d.date) }
}
</script>

<style lang="sass" scoped>

</style>
