<template>
    <div id="About" class="markdown-content" v-if="html">
        <div v-html="html" ref="markdownContainer"></div>

        <Collapse title="<span class='emoji'>🎓</span> Research papers">
            <ZoteroPublication v-for="item in publications" :key="item.key" :item="item"/>
        </Collapse>
    </div>
    <Loading v-else></Loading>
</template>

<script setup lang="ts">
import {onMounted, ref, nextTick} from 'vue'
import {marked} from 'marked';
import emojiRegex from 'emoji-regex';
import {parseExtensions} from '@/scripts/extended_markdown'
import ZoteroPublication from "@/components/ZoteroPublication.vue";
import Loading from "@/components/Loading.vue";
import Collapse from "@/components/Collapse.vue";
import {hosts, $} from "@/scripts/constants";
import {ZoteroAttachment, ZoteroItem} from "@/scripts/zotero";
import aboutMd from '@/data/about/README.md?raw';

const html = ref("")
const markdownContainer = ref<HTMLElement | null>(null)
const publications = ref<ZoteroItem[]>([])

onMounted(async (): Promise<void> => {
    html.value = await marked.parse(parseExtensions(aboutMd.replace(emojiRegex(), (emoji) => {
        return `<span class="emoji">${emoji}</span>`
    })))

    nextTick(() => {
        if (markdownContainer.value) {
            $(markdownContainer.value).find('.collapse-block').accordion({
                collapsible: true,
                header: '.collapse-header',
                heightStyle: 'content',
                active: false
            });

            // Fix image heights from attributes
            markdownContainer.value.querySelectorAll('img[height]').forEach(img => {
                const h = img.getAttribute('height');
                if (h) (img as HTMLElement).style.height = h.includes('px') ? h : h + 'px';
            });
        }
    })

    fetch(`${hosts.api}/zotero.json`)
        .then(it => it.json()).then((it: ZoteroItem[]) => {
            publications.value = it
            let files: ZoteroAttachment[] = it as unknown as ZoteroAttachment[]
            files = files.filter(file => file.data.itemType === 'attachment')
            publications.value = publications.value.filter(pub => pub.data.itemType !== 'attachment')
            publications.value.forEach(pub => pub.attachments = files.filter(a => a.data.parentItem == pub.key))
        })
})
</script>

<style lang="sass">
@use "../css/colors"

#About
    width: min(600px, 80vw)
    margin: auto
    padding-bottom: 100px
    padding-top: 20px

    .collapse-header
        margin: 0
        padding-top: 0.5em
        padding-bottom: 0.5em
        user-select: none
        border-bottom: none !important
        display: block

    .collapse-header:not(.ui-accordion-header-active)::after
        content: '...'
        margin-left: 0.25em

    .collapse-content
        padding-bottom: 0.5em

.emoji
    font-weight: normal

</style>
