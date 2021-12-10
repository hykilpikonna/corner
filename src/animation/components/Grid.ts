import * as THREE from "three";
import {GridHelper} from "three";
import {scene} from "@/animation/Home";
import IUpdatable from "@/animation/components/IUpdatable";

export default class Grid implements IUpdatable
{
    lines = []
    grid: GridHelper[] = []

    constructor()
    {
        const size = 100
        const divisions = 20

        let color = new THREE.Color('#7a0c0c')
        let grid = new THREE.GridHelper(size, divisions, color, color)
        grid.rotation.x = Math.PI / 2
        grid.position.z = 90
        this.grid.push(grid)

        color = new THREE.Color('#a4a4a4')
        grid = new THREE.GridHelper(size, divisions, color, color)
        grid.rotation.x = Math.PI / 2
        grid.position.z = 70
        this.grid.push(grid)

        this.grid.forEach(it => scene.add(it))
    }

    update(dt: number): void
    {
        // this.grid.rotation.x = 0.25 * Date.now() * 0.001
        // console.log(this.grid.rotation.x)
        // this.grid.position.z += dt * 10
        // console.log(this.grid.position.z)
    }
}
