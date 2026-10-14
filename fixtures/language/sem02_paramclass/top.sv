module sem02_top;
  import box_pkg::*;
  box #(8) b8 = new();
  box #(16, int) b16 = new();
  initial begin
    $display("%0d %0d", b8.width(), b16.width());
  end
endmodule
