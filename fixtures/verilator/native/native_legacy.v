`include "native_defs.vh"
module native_legacy_top(input clk, input [`NATIVE_WIDTH-1:0] d,
                         output [`NATIVE_WIDTH-1:0] q);
  native_leaf #(.WIDTH(`NATIVE_WIDTH)) u_leaf(.clk(clk), .d(d), .q(q));
endmodule

module native_leaf #(parameter WIDTH = 8)(input clk, input [WIDTH-1:0] d,
                                         output reg [WIDTH-1:0] q);
  always @(posedge clk) q <= d;
endmodule
