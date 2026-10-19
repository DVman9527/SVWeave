`include "demo_defs.svh"
`default_nettype none
module demo_top #(
  parameter int unsigned DATA_W = `SVW_DEFAULT_DATA_W,
  parameter int unsigned LANES  = 2
) (
  input  logic                          clk,
  input  logic                          rst_n,
  input  logic [LANES-1:0]               valid_i,
  output logic [LANES-1:0]               ready_o,
  input  logic [LANES-1:0][DATA_W-1:0]  data_i,
  output logic [LANES-1:0]               valid_o,
  input  logic [LANES-1:0]               ready_i,
  output logic [LANES-1:0][DATA_W-1:0]  data_o
);
  for (genvar lane = 0; lane < LANES; lane++) begin : g_lane
    demo_lane #(.DATA_W(DATA_W)) u_lane (
      .clk     (clk),
      .rst_n   (rst_n),
      .valid_i (valid_i[lane]),
      .ready_o (ready_o[lane]),
      .data_i  (data_i[lane]),
      .valid_o (valid_o[lane]),
      .ready_i (ready_i[lane]),
      .data_o  (data_o[lane])
    );
  end
endmodule
`default_nettype wire
