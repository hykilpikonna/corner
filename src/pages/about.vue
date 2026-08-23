<template>
    <div id="About" class="markdown-content" v-if="blocks.length">
        <template v-for="(block, index) in blocks" :key="index">
            <div v-if="!block.title" class="markdown-intro" v-html="block.html"></div>
            <Collapse v-else :title="encodeURIComponent(block.title)">
                <div v-html="block.html"></div>
            </Collapse>
        </template>

        <Collapse title="<span class='emoji'>🎓</span> Research papers">
            <ZoteroPublication v-for="item in publications" :key="item.key" :item="item"/>
        </Collapse>
    </div>
    <Loading v-else></Loading>
</template>

<script setup lang="ts">
import {marked} from 'marked';
import emojiRegex from 'emoji-regex';
import {parseExtensions} from '@/scripts/extended_markdown'
import ZoteroPublication from "@/components/ZoteroPublication.vue";
import Collapse from '@/components/Collapse.vue'
import {hosts} from "@/scripts/constants";
import Loading from "@/components/Loading.vue";
import type {ZoteroAttachment, ZoteroItem} from "@/scripts/zotero";

definePageMeta({title: '关于', navBookmark: 'about'})

const {data: blocks} = await useAsyncData('about-html', async () => {
    const markdown = await $fetch<string>(`${hosts.content}/README.md`)
    const parsed = parseExtensions(markdown.replace(emojiRegex(), (emoji) => {
        return `<span class="emoji">${emoji}</span>`
    }))

    return parsed.map(block => ({
        ...block,
        html: marked.parse(block.markdown, {async: false}),
    }))
}, {default: () => []})

const {data: publications} = await useAsyncData<ZoteroItem[]>('zotero-publications', async () => {
    const items = await $fetch<ZoteroItem[]>(`${hosts.api}/zotero.json`)
    const attachments = (items as unknown as ZoteroAttachment[])
        .filter(file => file.data.itemType === 'attachment')

    return items
        .filter(publication => publication.data.itemType !== 'attachment')
        .map(publication => ({
            ...publication,
            attachments: attachments.filter(attachment => attachment.data.parentItem === publication.key),
        }))
}, {default: () => []})

</script>

<style lang="sass">
@use "../css/colors"

#About
    width: min(600px, 80vw)
    margin: auto
    padding-bottom: 100px
    padding-top: 20px

    .markdown-intro > p:last-child
        margin-bottom: 0.7em

.emoji
    font-weight: normal

</style>
