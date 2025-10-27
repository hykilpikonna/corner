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

// Take in a string, use its hash to produce a number from 0 to 1
function detRandom(seed: string): number {
  return Array.from(seed).reduce((acc, char) => (acc + char.charCodeAt(0) * 65535) % 22859, 0) / 22859
}

@Component({})
export default class Photos extends Vue {
  photos: PhotoMetadata[]
  photoRows: PhotoMetadata[][]

  async created() {
    this.photos = await (await fetch('https://p.aza.moe/photos')).json()
    this.photos.sort((a, b) => (b.upload_time < a.upload_time ? 1 : -1))

    let rowProbabilityTable = {
      1: 0, 2: 0.5, 3: 0.5
    }

    // Generate photo rows: there is a 10% chance that a photo will be the only photo in its row
    this.photoRows = []
    let currentRow: PhotoMetadata[] = []
    this.photos.forEach((p) => {
      if (currentRow.length === 0) currentRow.push(p)
      else if (currentRow.length >= 3) {
        this.photoRows.push(currentRow)
        currentRow = [ p ]
      }
      else {
        const singleChance = detRandom(p.original_photo)
        if (singleChance < rowProbabilityTable[currentRow.length]) {
          this.photoRows.push(currentRow)
          currentRow = [ p ]
        } else currentRow.push(p)
      }
    })
    if (currentRow.length > 0) this.photoRows.push(currentRow)
  }

  url(s: string): string {
    s = s.replace('data/photos', 'static').replace('./', '')
    return `https://p.aza.moe/${s}`
  }

  randomRotation(s: string): string {
    const angle = (detRandom(s) * 20) - 10 // -10 to +10 degrees
    return `rotate(${angle}deg)`
  }
}
</script>

<template>
  <div class="title">
    <div class="font-script-en bold">The Wandering Gallery</div>
<!--    <div class="subtitle">Here I post pictures of the wonderful scenes I encounter during my trips.</div>-->
  </div>
  <div class="outer-grid">
    <div v-for="row in photoRows" :key="row[0].id" flex justify-center :class="`grid-cols-${row.length}`">
      <div v-for="p in row" :style="{transform: randomRotation(p.id)}" :key="p.id"
           class="img-container" relative>
        <img class="photo" w-full h-full object-contain :src="url(p.thumbnail_edited)" :alt="p.id"/>
        <div flex w-full justify-center absolute position-top-none>
          <img class="pin" src="/thumb%20tack%202%20plain.png" />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped lang="sass">
@use "../css/colors"
@use "../css/responsive"

.title
  margin-top: 8rem
  margin-bottom: 6rem

.bold
  font-size: 3em

.outer-grid
  gap: 0

.img-container
  filter: drop-shadow(0px 1px 2px rgba(0, 0, 0, 0.3))
  margin: -15px
  max-width: 50%

img.photo
  clip-path: inset(3.2% 1.8%)
  pointer-events: none

img.pin
  width: 40px
  height: 40px
</style>