resolutions=("128" "256")
initializations=("ones" "zeros" "rand")
for j in ${initializations[@]}
do
   for i in ${resolutions[@]} # OR ${list[*]}
   do
      CUDA_VISIBLE_DEVICES=5 ns-train gaussian-NeRF-bounded --experiment-name resolution-initialization-testing-$i-$j --data data/blender/lego --pipeline.model.f-init $j --pipeline.model.f-transition-function relu --pipeline.model.f-grid-resolution $i --pipeline.model.sigma 0.2 --pipeline.model.g-transition-function identity --pipeline.model.near-plane 2.0 --pipeline.model.far-plane 6.0 --pipeline.model.background-color white --vis wandb blender-data
   done
done