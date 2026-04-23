<script lang="ts">
import {defineComponent, h} from 'vue'

export default defineComponent({
  name: 'Dynamic',
  props: {
    template: {
      type: String,
      required: true
    }
  },
  render() {
    return h({template: this.template})
  }
})
</script>
