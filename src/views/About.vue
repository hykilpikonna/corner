<template>
    <div id="About" class="markdown-content" v-if="html">
        <Dynamic :template="html"></Dynamic>

        <Collapse title="<span class='emoji'>🎓</span> Research papers">
            <ZoteroPublication v-for="item in publications" :key="item.key" :item="item"/>
        </Collapse>
    </div>
    <Loading v-else></Loading>
</template>

<script setup lang="ts">
import {onMounted, ref} from 'vue'
import {marked} from 'marked';
import emojiRegex from 'emoji-regex';
import {parseExtensions} from '@/scripts/extended_markdown'
import ZoteroPublication from "@/components/ZoteroPublication.vue";
import {hosts} from "@/scripts/constants";
import Loading from "@/components/Loading.vue";
import {ZoteroAttachment, ZoteroItem} from "@/scripts/zotero";

const html = ref("")
const publications = ref<ZoteroItem[]>([])

onMounted((): void => {
    fetch(`${hosts.content}/README.md`).then(it => it.text())
        .then(it => html.value = marked(parseExtensions(it.replace(emojiRegex(), (emoji) => {
            return `<span class="emoji">${emoji}</span>`
        }))))

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

.emoji
    font-weight: normal

</style>
