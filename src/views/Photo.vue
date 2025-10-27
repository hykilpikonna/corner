<script lang="ts">
import { Vue, Component } from 'vue-facing-decorator';

interface PhotoMetadata {
  id: string
  owner_key: string // This will be filtered out for public requests
  upload_time: string
  original_photo: string
  edited_photo: string
  thumbnail: string
  thumbnail_edited: string
  exif: {[id: string]: string}
}

@Component({})
export default class Photos extends Vue {
  photos: PhotoMetadata[]
  photoRows: PhotoMetadata[][]

  async created() {
    this.photos = await (await fetch('https://p.aza.moe/photos')).json()
    this.photos.sort((a, b) => (b.upload_time < a.upload_time ? 1 : -1))

    // Generate photo rows: there is a 10% chance that a photo will be the only photo in its row
    this.photoRows = []
    let currentRow: PhotoMetadata[] = []
    this.photos.forEach((p) => {
      if (currentRow.length === 0) {
        currentRow.push(p)
      } else {
        const singleChance = Math.random()
        if (singleChance < 0.1) {
          this.photoRows.push(currentRow)
          currentRow = [ p ]
        } else {
          currentRow.push(p)
        }
      }
    })
  }

  url(s: string): string {
    s = s.replace('data/photos', 'static').replace('./', '')
    return `https://p.aza.moe/${s}`
  }

  randomRotation(s: string): string {
    const hash = Array.from(s).reduce((acc, char) => acc + char.charCodeAt(0), 0)
    const angle = (hash % 21) - 10 // -10 to +10 degrees
    return `rotate(${angle}deg)`
  }
}
</script>

<template>
  <div class="header mb-10">
    <h1 class="font-script-en bold">The Wandering Gallery</h1>
    <div>Here I post pictures of the wonderful scenes I encounter during my trips.</div>
  </div>
  <div class="outer-grid grid grid-cols-2 gap-2 p-2">
    <div v-for="p in photos" class="img-container" :style="{transform: randomRotation(p.id)}" :key="p.id">
      <img class="w-full h-full object-contain" :src="url(p.thumbnail_edited)" :alt="p.id"/>
    </div>
  </div>
</template>

<style scoped lang="sass">
@use "../css/colors"

h1
  font-size: 3em

.outer-grid
  gap: 0

.img-container
  filter: drop-shadow(0px 1px 2px rgba(0, 0, 0, 0.3))
  margin: -15px

img
  clip-path: inset(10px 8px)
  pointer-events: none
</style>