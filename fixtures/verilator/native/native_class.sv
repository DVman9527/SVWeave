typedef logic [3:0] nibble_t;
class Packet;
  nibble_t payload;
  function int get_payload();
    return int'(payload);
  endfunction
endclass

module native_class_top(output int result);
  Packet packet;
  initial begin
    packet = new();
    packet.payload = 4'h3;
    result = packet.get_payload();
  end
endmodule
