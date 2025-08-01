#pragma once

#include "cutlass/cutlass.h"

#include "cutlass/gemm/collective/collective_builder.hpp"
#include "cutlass/epilogue/collective/collective_builder.hpp"
#include "cutlass/gemm/device/gemm_universal_adapter.h"

#include "cutlass_extensions/epilogue/scaled_mm_epilogues_c3x.hpp"
#include "cutlass_extensions/common.hpp"
#include "get_group_starts.cuh"

using namespace cute;

namespace {

using ProblemShape =
    cutlass::gemm::GroupProblemShape<cute::Shape<int, int, int>>;

using ElementAccumulator = float;
using OperatorClass = cutlass::arch::OpClassTensorOp;

using LayoutA = cutlass::layout::RowMajor;
using LayoutA_Transpose =
    typename cutlass::layout::LayoutTranspose<LayoutA>::type;
using LayoutB = cutlass::layout::ColumnMajor;
using LayoutB_Transpose =
    typename cutlass::layout::LayoutTranspose<LayoutB>::type;
using LayoutD = cutlass::layout::RowMajor;
using LayoutD_Transpose =
    typename cutlass::layout::LayoutTranspose<LayoutD>::type;
using LayoutC = LayoutD;
using LayoutC_Transpose = LayoutD_Transpose;

template <typename ElementAB_, typename ElementC_, typename ArchTag_,
          template <typename, typename, typename> typename Epilogue_,
          typename TileShape, typename ClusterShape, typename KernelSchedule,
          typename EpilogueSchedule, bool swap_ab_ = false>
struct cutlass_3x_group_gemm {
  static constexpr bool swap_ab = swap_ab_;
  using ElementAB = ElementAB_;
  using ElementC = void;
  using ElementD = ElementC_;
  using ElementAccumulator = float;
  using ArchTag = ArchTag_;

  using Epilogue = Epilogue_<ElementAccumulator, ElementD, TileShape>;

  static constexpr int AlignmentAB =
      128 / cutlass::sizeof_bits<ElementAB>::value;
  static constexpr int AlignmentC = 128 / cutlass::sizeof_bits<ElementD>::value;

  using EVTCompute = typename Epilogue::EVTCompute;

  using CollectiveEpilogue =
      typename cutlass::epilogue::collective::CollectiveBuilder<
          ArchTag, OperatorClass, TileShape, ClusterShape,
          cutlass::epilogue::collective::EpilogueTileAuto, ElementAccumulator,
          ElementAccumulator, ElementC,
          conditional_t<swap_ab, LayoutC_Transpose*, LayoutC*>, AlignmentC,
          ElementD, conditional_t<swap_ab, LayoutD_Transpose*, LayoutD*>,
          AlignmentC, EpilogueSchedule, EVTCompute>::CollectiveOp;

  static constexpr size_t CEStorageSize =
      sizeof(typename CollectiveEpilogue::SharedStorage);
  using Stages = typename cutlass::gemm::collective::StageCountAutoCarveout<
      static_cast<int>(CEStorageSize)>;

  using CollectiveMainloop = conditional_t<
      swap_ab,
      typename cutlass::gemm::collective::CollectiveBuilder<
          ArchTag, OperatorClass, ElementAB, LayoutB_Transpose*, AlignmentAB,
          ElementAB, LayoutA_Transpose*, AlignmentAB, ElementAccumulator,
          TileShape, ClusterShape, Stages, KernelSchedule>::CollectiveOp,
      typename cutlass::gemm::collective::CollectiveBuilder<
          ArchTag, OperatorClass, ElementAB, LayoutA*, AlignmentAB, ElementAB,
          LayoutB*, AlignmentAB, ElementAccumulator, TileShape, ClusterShape,
          Stages, KernelSchedule>::CollectiveOp>;

  using KernelType = enable_sm90_or_later<cutlass::gemm::kernel::GemmUniversal<
      ProblemShape, CollectiveMainloop, CollectiveEpilogue>>;

  struct GemmKernel : public KernelType {};
};

template <typename Gemm>
void cutlass_group_gemm_caller(
    torch::Tensor& out_tensors, torch::Tensor const& a_tensors,
    torch::Tensor const& b_tensors, torch::Tensor const& a_scales,
    torch::Tensor const& b_scales, torch::Tensor const& expert_offsets,
    torch::Tensor const& problem_sizes, torch::Tensor const& a_strides,
    torch::Tensor const& b_strides, torch::Tensor const& c_strides,
    bool per_act_token, bool per_out_ch) {
  static constexpr bool swap_ab = Gemm::swap_ab;

  using ElementAB = typename Gemm::ElementAB;
  using ElementD = typename Gemm::ElementD;

  int num_experts = static_cast<int>(expert_offsets.size(0));

  auto stream = at::cuda::getCurrentCUDAStream(a_tensors.device().index());

  auto options_int =
      torch::TensorOptions().dtype(torch::kInt64).device(a_tensors.device());

  torch::Tensor a_ptrs = torch::empty(num_experts, options_int);
  torch::Tensor b_ptrs = torch::empty(num_experts, options_int);
  torch::Tensor out_ptrs = torch::empty(num_experts, options_int);
  torch::Tensor a_scales_ptrs = torch::empty(num_experts, options_int);
  torch::Tensor b_scales_ptrs = torch::empty(num_experts, options_int);

  run_get_group_gemm_starts(expert_offsets, a_ptrs, b_ptrs, out_ptrs,
                            a_scales_ptrs, b_scales_ptrs, a_tensors, b_tensors,
                            out_tensors, a_scales, b_scales);

  using GemmKernel = typename Gemm::GemmKernel;
  using StrideA = Stride<int64_t, Int<1>, Int<0>>;
  using StrideB = Stride<int64_t, Int<1>, Int<0>>;
  using StrideC = typename GemmKernel::InternalStrideC;

  ProblemShape::UnderlyingProblemShape* problem_sizes_as_shapes =
      static_cast<ProblemShape::UnderlyingProblemShape*>(
          problem_sizes.data_ptr());
  ProblemShape prob_shape{num_experts, problem_sizes_as_shapes, nullptr};

  typename GemmKernel::MainloopArguments mainloop_args;
  if constexpr (swap_ab) {
    mainloop_args = typename GemmKernel::MainloopArguments{
        static_cast<const ElementAB**>(b_ptrs.data_ptr()),
        static_cast<StrideB*>(b_strides.data_ptr()),
        static_cast<const ElementAB**>(a_ptrs.data_ptr()),
        static_cast<StrideA*>(a_strides.data_ptr())};
  } else {
    mainloop_args = typename GemmKernel::MainloopArguments{
        static_cast<const ElementAB**>(a_ptrs.data_ptr()),
        static_cast<StrideA*>(a_strides.data_ptr()),
        static_cast<const ElementAB**>(b_ptrs.data_ptr()),
        static_cast<StrideB*>(b_strides.data_ptr())};
  }

  // Currently, we are only able to do broadcast on either all or none a_scales
  // and on either all or none b_scales
  typename GemmKernel::EpilogueArguments epilogue_args{
      Gemm::Epilogue::prepare_args(
          swap_ab ? static_cast<const ElementAccumulator**>(
                        b_scales_ptrs.data_ptr())
                  : static_cast<const ElementAccumulator**>(
                        a_scales_ptrs.data_ptr()),
          swap_ab ? static_cast<const ElementAccumulator**>(
                        a_scales_ptrs.data_ptr())
                  : static_cast<const ElementAccumulator**>(
                        b_scales_ptrs.data_ptr()),
          swap_ab ? per_out_ch : per_act_token,
          swap_ab ? per_act_token : per_out_ch),
      nullptr, static_cast<StrideC*>(c_strides.data_ptr()),
      static_cast<ElementD**>(out_ptrs.data_ptr()),
      static_cast<StrideC*>(c_strides.data_ptr())};

  int device_id = a_tensors.device().index();
  static const cutlass::KernelHardwareInfo hw_info{
      device_id, cutlass::KernelHardwareInfo::query_device_multiprocessor_count(
                     device_id)};

  typename GemmKernel::Arguments args{
      cutlass::gemm::GemmUniversalMode::kGrouped, prob_shape, mainloop_args,
      epilogue_args, hw_info};

  using GemmOp = cutlass::gemm::device::GemmUniversalAdapter<GemmKernel>;
  GemmOp gemm_op;
  CUTLASS_CHECK(gemm_op.can_implement(args));

  size_t workspace_size = gemm_op.get_workspace_size(args);
  auto const workspace_options =
      torch::TensorOptions().dtype(torch::kUInt8).device(a_tensors.device());
  auto workspace = torch::empty(workspace_size, workspace_options);

  cutlass::Status status = gemm_op.run(args, workspace.data_ptr(), stream);
  CUTLASS_CHECK(status);
}

}  // namespace
