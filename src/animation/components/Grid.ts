import * as THREE from "three";
import {GridHelper} from "three";
import {scene} from "@/animation/Home";
import IUpdatable from "@/animation/components/IUpdatable";

export default class Grid implements IUpdatable
{
    lines = []
    grid: GridHelper

    constructor()
    {
        const size = 100
        const divisions = 10

        this.grid = new THREE.GridHelper(size, divisions)
        this.grid.rotation.x = Math.PI / 2
        scene.add(this.grid)
    }

    update(dt: number): void
    {
        // this.grid.rotation.x = 0.25 * Date.now() * 0.001
        // console.log(this.grid.rotation.x)
    }
}
