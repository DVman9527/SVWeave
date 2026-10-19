module stub_top(input logic clk, input logic [7:0] d, output logic [7:0] q);
  vendor_ip u_ip(.clk(clk), .d(d), .q(q));
endmodule
