<script lang="ts">
import { defineComponent, h } from 'vue';
import Collapse from './Collapse.vue';
import Tag from './Tag.vue';
import BlogIndex from './BlogIndex.vue';
import ZoteroPublication from './ZoteroPublication.vue';

export default defineComponent({
  name: 'Dynamic',
  props: {
    template: {
      type: String,
      required: true
    }
  },
  render() {
    return h({
      template: this.template,
      components: {
        Collapse,
        Tag,
        BlogIndex,
        ZoteroPublication
      }
    });
  }
});
</script>
