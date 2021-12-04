<template>
    <div id="Home" class="fbox-center f-grow1">
        <div id="box">
            <div class="font-script-en">Hykilpikonna's</div>
            <div class="font-script-en bold">Road Less Traveled</div>
        </div>
    </div>
</template>

<script lang="ts">
import {Options, Vue} from 'vue-class-component';

@Options({components: {}})
export default class Home extends Vue
{

}
</script>

<style lang="sass" scoped>
#box
    font-size: 3em

#Home
    text-align: left
</style>
