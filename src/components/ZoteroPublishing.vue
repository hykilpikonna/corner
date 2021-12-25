<template>
    <div id="Publishing">

    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';

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
    data: {
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
}

@Options({components: {}})
export default class Publishing extends Vue
{

}
</script>

<style lang="sass" scoped>

</style>
